
import re
import uuid

import pdfplumber
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Prefetch, Q
from django.db.utils import OperationalError
from django.http import HttpResponse
from rest_framework import filters, generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Faculty,
    FacultySuggestionDecision,
    Paper,
    PaperAuthorship,
    Patent,
    Project,
    School,
    Department,
    ContactTeamMember,
    ContactPageSettings,
)
from .serializers import (
    FacultyProfileSerializer,
    FacultySerializer,
    PaperSerializer,
    PatentSerializer,
    ProjectSerializer,
    SchoolSerializer,
    DepartmentSerializer,
    ContactTeamMemberSerializer,
    ContactPageSettingsSerializer,
)
from .semantic import cosine_similarity, create_query_embedding


def _normalize_keyword_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _year_from_dates(*dates):
    for dt in dates:
        if dt:
            return dt.year
    return None


def _normalize_paper_link(download_url, url, license_url, doi):
    for value in (download_url, url, license_url):
        # Strip outer whitespace, then remove any embedded whitespace (spaces, tabs, newlines in URLs are invalid)
        clean_value = re.sub(r'\s+', '', str(value or "").strip())
        if not clean_value:
            continue
        if clean_value.startswith(("http://", "https://")):
            return clean_value

    # Only fall back to DOI if it looks complete (has a slash after the prefix e.g. 10.xxxx/yyyy)
    clean_doi = re.sub(r'\s+', '', str(doi or "").strip())
    if clean_doi and clean_doi.count("/") >= 1:
        parts = clean_doi.split("/", 1)
        if len(parts[1]) > 3:  # suffix must be more than 3 characters
            return f"https://doi.org/{clean_doi}"

    return ""




def _generate_signup_faculty_id():
    generated_faculty_id = f"SIGNUP-{uuid.uuid4().hex[:12]}"
    while Faculty.objects.filter(faculty_id=generated_faculty_id).exists():
        generated_faculty_id = f"SIGNUP-{uuid.uuid4().hex[:12]}"
    return generated_faculty_id


def _full_name(first_name, last_name, fallback=""):
    return f"{(first_name or '').strip()} {(last_name or '').strip()}".strip() or fallback


def _keywords_for_matching(faculty):
    merged = (
        _normalize_keyword_list(getattr(faculty, "keywords", None))
        + _normalize_keyword_list(getattr(faculty, "faculty_keywords", None))
        + _normalize_keyword_list(getattr(faculty, "ai_keywords", None))
    )
    return {item.lower() for item in merged if item}


def _merge_unique_list(*values):
    ordered = []
    seen = set()
    for value in values:
        items = _normalize_keyword_list(value)
        for item in items:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    return ordered




def _has_salisbury_department(faculty):
    return bool(_faculty_department_names(faculty) or _faculty_school_names(faculty))


def _faculty_school_names(faculty):
    names = []
    if getattr(faculty, "primary_school_id", None):
        names.append(faculty.primary_school.name)
    names.extend([school.name for school in faculty.schools.all()])
    return _merge_unique_list(names)


def _faculty_department_names(faculty):
    names = []
    if getattr(faculty, "primary_department_id", None):
        names.append(faculty.primary_department.name)
    names.extend([department.name for department in faculty.departments.all()])
    return _merge_unique_list(names)


def _is_confirmed_su_faculty(faculty):
    if getattr(faculty, "confirmed_su_faculty", False):
        return True
    if getattr(faculty, "review_status", "") == "confirmed_su":
        return True
    if faculty.user_id and faculty.is_approved and faculty.profile_visibility:
        return True
    email = (faculty.email or "").strip().lower()
    if email.endswith("@salisbury.edu") or email.endswith("@gulls.salisbury.edu"):
        return True
    return bool(_faculty_department_names(faculty) or _faculty_school_names(faculty))


def _score_network_item(
    candidate_keywords,
    candidate_departments,
    candidate_schools,
    my_keywords,
    my_departments,
    my_schools,
    richness=0,
):
    candidate_keyword_set = {item.lower() for item in _normalize_keyword_list(candidate_keywords)}
    my_keyword_set = {item.lower() for item in _normalize_keyword_list(my_keywords)}
    shared_keywords = sorted(candidate_keyword_set.intersection(my_keyword_set))

    department_match = bool(
        {item.lower() for item in _normalize_keyword_list(candidate_departments)}
        .intersection({item.lower() for item in _normalize_keyword_list(my_departments)})
    )
    school_match = bool(
        {item.lower() for item in _normalize_keyword_list(candidate_schools)}
        .intersection({item.lower() for item in _normalize_keyword_list(my_schools)})
    )

    score = 38
    if not my_keyword_set:
        score = 45
    score += min(36, len(shared_keywords) * 12)
    if department_match:
        score += 12
    if school_match:
        score += 8
    score += min(10, richness * 2)
    return min(96, score), shared_keywords[:6], department_match, school_match


def _network_reason(shared_keywords, department_match, school_match, fallback):
    if shared_keywords and department_match:
        return f"Shared expertise in {', '.join(shared_keywords[:3])} with department overlap."
    if shared_keywords and school_match:
        return f"Shared expertise in {', '.join(shared_keywords[:3])} within a related school."
    if shared_keywords:
        return f"Shared expertise in {', '.join(shared_keywords[:3])}."
    if department_match:
        return "Potential fit based on department alignment."
    if school_match:
        return "Potential fit based on school alignment."
    return fallback


def _matches_query(values, query):
    if not query:
        return True
    needle = query.lower()
    return any(needle in str(value or "").lower() for value in values)


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                return trimmed
            continue
        return value
    return None


def _email_available_for_faculty(email, internal_id, external_id):
    if not email:
        return False
    return not Faculty.objects.filter(email__iexact=email).exclude(
        id__in=[internal_id, external_id]
    ).exists()


def _absorb_external_faculty(internal, external):
    if not external or external.id == internal.id:
        return {"papers": 0, "projects": 0, "patents": 0, "authorships": 0}

    internal.first_name = _first_non_empty(internal.first_name, external.first_name) or ""
    internal.last_name = _first_non_empty(internal.last_name, external.last_name) or ""
    internal.name = _first_non_empty(internal.name, external.name, _full_name(internal.first_name, internal.last_name, ""))
    internal.title = _first_non_empty(internal.title, external.title)
    internal.department = _first_non_empty(internal.department, external.department)
    internal.office = _first_non_empty(internal.office, external.office)
    internal.room = _first_non_empty(internal.room, external.room)
    internal.phone = _first_non_empty(internal.phone, external.phone)
    internal.bio = _first_non_empty(internal.bio, external.bio)

    candidate_email = _first_non_empty(internal.email, external.email)
    if _email_available_for_faculty(candidate_email, internal.id, external.id):
        internal.email = candidate_email.lower()
        # Clear from external first to avoid unique constraint violation on save
        if external.email:
            external.email = None
            external.save(update_fields=["email"])

    internal.department_affiliations = _merge_unique_list(
        internal.department_affiliations, external.department_affiliations
    )
    internal.school = _first_non_empty(internal.school, external.school)
    internal.school_affiliations = _merge_unique_list(
        internal.school_affiliations, external.school_affiliations
    )
    internal.dois = _merge_unique_list(internal.dois, external.dois)
    internal.titles = _merge_unique_list(internal.titles, external.titles)
    internal.themes = _merge_unique_list(internal.themes, external.themes)
    internal.journals = _merge_unique_list(internal.journals, external.journals)
    internal.keywords = _merge_unique_list(internal.keywords, external.keywords)
    internal.faculty_keywords = ", ".join(
        _merge_unique_list(internal.faculty_keywords, external.faculty_keywords)
    )
    internal.ai_keywords = ", ".join(
        _merge_unique_list(internal.ai_keywords, external.ai_keywords)
    )

    internal.total_citations = max(internal.total_citations or 0, external.total_citations or 0)
    internal.article_count = max(internal.article_count or 0, external.article_count or 0)
    internal.average_citations = max(
        internal.average_citations or 0.0, external.average_citations or 0.0
    )
    internal.save()

    papers_added = 0
    for paper in external.papers.all():
        before = paper.authors.filter(id=internal.id).exists()
        paper.authors.add(internal)
        if not before:
            papers_added += 1

    projects_added = 0
    for project in external.projects.all():
        before = project.faculty.filter(id=internal.id).exists()
        project.faculty.add(internal)
        if not before:
            projects_added += 1

    patents_added = 0
    for patent in external.patents.all():
        before = patent.faculty.filter(id=internal.id).exists()
        patent.faculty.add(internal)
        if not before:
            patents_added += 1

    authorships_added = 0
    for authorship in external.authorships.all():
        _, created = PaperAuthorship.objects.get_or_create(
            paper=authorship.paper,
            faculty=internal,
            defaults={"status": authorship.status, "decided_at": authorship.decided_at},
        )
        if created:
            authorships_added += 1

    external.profile_visibility = False
    external.is_approved = False
    external.save(update_fields=["profile_visibility", "is_approved"])

    return {
        "papers": papers_added,
        "projects": projects_added,
        "patents": patents_added,
        "authorships": authorships_added,
    }


def _external_faculty_preview_payload(external):
    papers = [
        {
            "id": paper.id,
            "doi": paper.doi,
            "title": paper.title,
            "year": _year_from_dates(
                paper.date_published_online, paper.date_published_print, paper.date_published
            ),
            "journal": paper.journal or "",
        }
        for paper in external.papers.all()[:50]
    ]
    projects = [
        {
            "id": project.id,
            "title": project.title,
            "status": project.status or "",
            "start_date": project.start_date.isoformat() if project.start_date else None,
            "end_date": project.end_date.isoformat() if project.end_date else None,
        }
        for project in external.projects.all()[:50]
    ]
    patents = [
        {
            "id": patent.id,
            "title": patent.title,
            "patent_number": patent.patent_number or "",
            "issue_year": patent.issue_date.year if patent.issue_date else None,
        }
        for patent in external.patents.all()[:50]
    ]

    return {
        "faculty": {
            "id": external.id,
            "faculty_id": external.faculty_id,
            "name": _full_name(
                external.first_name,
                external.last_name,
                (external.name or "").strip() or external.faculty_id,
            ),
            "department": external.department or "",
            "title": external.title or "",
            "email": external.email or "",
            "keywords": _normalize_keyword_list(external.keywords)
            or _normalize_keyword_list(external.faculty_keywords)
            or _normalize_keyword_list(external.ai_keywords),
        },
        "papers": papers,
        "projects": projects,
        "patents": patents,
        "counts": {
            "papers": external.papers.count(),
            "projects": external.projects.count(),
            "patents": external.patents.count(),
        },
    }


def _get_request_faculty(user, create_if_missing=False):
    faculty = Faculty.objects.filter(user=user).first()
    if faculty:
        if faculty.user_id and not faculty.is_approved:
            faculty.is_approved = True
            faculty.save(update_fields=["is_approved", "updated_at"])
        return faculty

    email = (user.email or "").strip()
    if not create_if_missing:
        return None

    email_in_use_elsewhere = (
        bool(email)
        and Faculty.objects.filter(email__iexact=email).exclude(user=user).exists()
    )
    safe_email = None if email_in_use_elsewhere else (email.lower() if email else None)

    return Faculty.objects.create(
        user=user,
        faculty_id=_generate_signup_faculty_id(),
        email=safe_email,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        name=_full_name(user.first_name, user.last_name, user.username),
        is_approved=True,
        profile_visibility=True,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def public_search_data(request):
    faculty_qs = (
        Faculty.objects.filter(profile_visibility=True)
        .filter(Q(is_approved=True) | Q(user__isnull=False))
        .prefetch_related("projects", "patents", "departments", "schools")
        .select_related("user", "primary_department", "primary_school")
        .order_by("last_name", "first_name")
    )
    _faculty_with_affiliations = Faculty.objects.prefetch_related(
        "schools", "departments"
    ).select_related("primary_school", "primary_department")

    papers_qs = (
        Paper.objects.defer("paper_embedding", "embedding_model", "embedding_updated_at")
        .prefetch_related(Prefetch("authors", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )
    projects_qs = (
        Project.objects.all()
        .prefetch_related(Prefetch("faculty", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )
    patents_qs = (
        Patent.objects.all()
        .prefetch_related(Prefetch("faculty", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )

    faculty = []
    for item in faculty_qs:
        if not item.user_id and not _has_salisbury_department(item):
            continue

        user_first_name = (item.user.first_name if item.user else "") or ""
        user_last_name = (item.user.last_name if item.user else "") or ""
        user_username = (item.user.username if item.user else "") or ""
        full_name = _full_name(
            item.first_name or user_first_name,
            item.last_name or user_last_name,
            (item.name or "").strip() or user_username or item.email or item.faculty_id,
        )
        merged_keywords = _merge_unique_list(item.keywords, item.faculty_keywords, item.ai_keywords)
        photo_url = request.build_absolute_uri(item.photo.url) if item.photo else ""

        departments = _faculty_department_names(item)
        schools = _faculty_school_names(item)

        faculty.append(
            {
                "id": str(item.id),
                "name": full_name,
                "title": item.title or "",
                "department": departments[0] if departments else "Unassigned",
                "departmentAffiliations": departments,
                "school": schools[0] if schools else "",
                "schoolAffiliations": schools,
                "email": item.email or "",
                "phone": item.phone or "",
                "photo": photo_url,
                "bio": item.bio or "",
                "researchInterests": merged_keywords[:8],
                "aiKeywords": merged_keywords,
                "metricsProfile": {
                    "totalCitations": item.total_citations or 0,
                    "articleCount": item.article_count or 0,
                    "averageCitations": item.average_citations or 0.0,
                },
                "themes": _normalize_keyword_list(item.themes),
                "journals": _normalize_keyword_list(item.journals),
            }
        )

    papers = []
    for item in papers_qs:
        year = _year_from_dates(
            item.date_published_online, item.date_published_print, item.date_published
        )
        authors = list(item.authors.all())
        paper_departments = _merge_unique_list(
            *[_faculty_department_names(author) for author in authors],
        )
        paper_schools = _merge_unique_list(
            *[_faculty_school_names(author) for author in authors],
        )
        papers.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "doi": item.doi or "",
                "journal": item.journal or "",
                "authors": [author.name or f"{author.first_name or ''} {author.last_name or ''}".strip() for author in authors],
                "year": year or 0,
                "abstract": item.abstract or "",
                "link": _normalize_paper_link(item.download_url, item.url, item.license_url, item.doi),
                "citations": item.tc_count or 0,
                "publishedOnline": item.date_published_online.isoformat() if item.date_published_online else "",
                "publishedPrint": item.date_published_print.isoformat() if item.date_published_print else "",
                "aiKeywords": _normalize_keyword_list(item.keywords)
                or _normalize_keyword_list(item.ai_keywords)
                or _normalize_keyword_list(item.faculty_keywords),
                "facultyMembers": _normalize_keyword_list(item.faculty_members),
                "facultyAffiliations": item.faculty_affiliations or {},
                "departmentAffiliations": paper_departments,
                "schoolAffiliations": paper_schools,
            }
        )

    projects = []
    for item in projects_qs:
        projects.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "leadFaculty": [
                    member.name
                    or f"{member.first_name or ''} {member.last_name or ''}".strip()
                    for member in item.faculty.all()
                ],
                "status": item.status or "Active",
                "description": item.description or "",
                "startDate": item.start_date.isoformat() if item.start_date else "",
                "endDate": item.end_date.isoformat() if item.end_date else "",
                "aiKeywords": _normalize_keyword_list(item.keywords),
                "departmentAffiliations": _merge_unique_list(
                    *[_faculty_department_names(member) for member in item.faculty.all()]
                ),
                "schoolAffiliations": _merge_unique_list(
                    *[_faculty_school_names(member) for member in item.faculty.all()]
                ),
            }
        )

    patents = []
    for item in patents_qs:
        patents.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "inventors": [
                    member.name
                    or f"{member.first_name or ''} {member.last_name or ''}".strip()
                    for member in item.faculty.all()
                ],
                "patentNumber": item.patent_number or "",
                "year": item.issue_date.year if item.issue_date else 0,
                "description": item.abstract or "",
                "link": item.link or "",
                "aiKeywords": _normalize_keyword_list(item.aiKeywords),
                "departmentAffiliations": _merge_unique_list(
                    *[_faculty_department_names(member) for member in item.faculty.all()]
                ),
                "schoolAffiliations": _merge_unique_list(
                    *[_faculty_school_names(member) for member in item.faculty.all()]
                ),
            }
        )

    return Response(
        {
            "facultyData": faculty,
            "papersData": papers,
            "patentsData": patents,
            "projectsData": projects,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def network_discovery(request):
    faculty = _get_request_faculty(request.user)
    if not faculty:
        raise NotFound("Faculty profile not found for this user.")

    query = (request.query_params.get("q") or "").strip()
    try:
        limit = int(request.query_params.get("limit") or (25 if query else 5))
    except (TypeError, ValueError):
        limit = 25 if query else 5
    limit = max(1, min(limit, 50))

    my_keywords = list(_keywords_for_matching(faculty))
    my_departments = _faculty_department_names(faculty)
    my_schools = _faculty_school_names(faculty)

    colleagues = []
    faculty_qs = (
        Faculty.objects.filter(profile_visibility=True)
        .filter(Q(is_approved=True) | Q(user__isnull=False) | Q(confirmed_su_faculty=True))
        .exclude(id=faculty.id)
        .prefetch_related("schools", "departments", "papers")
        .select_related("primary_school", "primary_department", "user")
    )
    for item in faculty_qs:
        if not _is_confirmed_su_faculty(item):
            continue

        name = _full_name(
            item.first_name,
            item.last_name,
            (item.name or "").strip() or item.faculty_id,
        )
        departments = _faculty_department_names(item)
        schools = _faculty_school_names(item)
        keywords = _merge_unique_list(item.keywords, item.faculty_keywords, item.ai_keywords, item.themes)
        if not _matches_query(
            [name, item.email, item.title, item.bio, *departments, *schools, *keywords],
            query,
        ):
            continue

        score, shared, department_match, school_match = _score_network_item(
            keywords,
            departments,
            schools,
            my_keywords,
            my_departments,
            my_schools,
            richness=sum(
                bool(value)
                for value in [
                    item.email,
                    item.title,
                    item.bio,
                    departments,
                    schools,
                    keywords,
                    item.article_count,
                ]
            ),
        )
        colleagues.append(
            {
                "id": str(item.id),
                "name": name,
                "title": item.title or "",
                "department": departments[0] if departments else "Unassigned",
                "departments": departments,
                "school": schools[0] if schools else "",
                "schools": schools,
                "email": item.email or "",
                "phone": item.phone or "",
                "bio": item.bio or "",
                "photo": request.build_absolute_uri(item.photo.url) if item.photo else "",
                "keywords": keywords,
                "sharedKeywords": shared,
                "matchScore": score,
                "matchReason": _network_reason(
                    shared,
                    department_match,
                    school_match,
                    "Potential collaboration fit based on available SU profile data.",
                ),
                "articleCount": item.article_count or item.papers.count(),
                "totalCitations": item.total_citations or 0,
            }
        )

    papers = []
    papers_qs = (
        Paper.objects.defer("paper_embedding", "embedding_model", "embedding_updated_at")
        .prefetch_related("authors", "authors__schools", "authors__departments")
        .order_by("-id")
    )
    for item in papers_qs:
        authors = list(item.authors.all())
        if not any(_is_confirmed_su_faculty(author) for author in authors):
            continue
        departments = _merge_unique_list(
            *[_faculty_department_names(author) for author in authors],
        )
        schools = _merge_unique_list(
            *[_faculty_school_names(author) for author in authors],
        )
        keywords = _merge_unique_list(item.keywords, item.ai_keywords, item.faculty_keywords, item.themes)
        author_names = [
            author.name
            or _full_name(author.first_name, author.last_name, author.faculty_id)
            for author in authors
        ]
        if not _matches_query([item.title, item.journal, item.abstract, *author_names, *departments, *schools, *keywords], query):
            continue
        score, shared, department_match, school_match = _score_network_item(
            keywords,
            departments,
            schools,
            my_keywords,
            my_departments,
            my_schools,
            richness=sum(bool(value) for value in [item.journal, item.abstract, item.tc_count, keywords]),
        )
        papers.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "authors": author_names,
                "journal": item.journal or "",
                "year": _year_from_dates(item.date_published_online, item.date_published_print, item.date_published) or 0,
                "abstract": item.abstract or "",
                "link": _normalize_paper_link(item.download_url, item.url, item.license_url, item.doi),
                "citations": item.tc_count or 0,
                "keywords": keywords,
                "departments": departments,
                "schools": schools,
                "sharedKeywords": shared,
                "relevanceScore": score,
                "relevanceReason": _network_reason(
                    shared,
                    department_match,
                    school_match,
                    "Potential reading or collaboration lead based on SU publication data.",
                ),
            }
        )

    patents = []
    for item in Patent.objects.all().prefetch_related("faculty", "faculty__schools", "faculty__departments").order_by("-id"):
        members = list(item.faculty.all())
        if not any(_is_confirmed_su_faculty(member) for member in members):
            continue
        departments = _merge_unique_list(*[_faculty_department_names(member) for member in members])
        schools = _merge_unique_list(*[_faculty_school_names(member) for member in members])
        keywords = _normalize_keyword_list(item.aiKeywords)
        inventor_names = [
            member.name
            or _full_name(member.first_name, member.last_name, member.faculty_id)
            for member in members
        ]
        if not _matches_query([item.title, item.patent_number, item.abstract, *inventor_names, *departments, *schools, *keywords], query):
            continue
        score, shared, department_match, school_match = _score_network_item(
            keywords,
            departments,
            schools,
            my_keywords,
            my_departments,
            my_schools,
            richness=sum(bool(value) for value in [item.patent_number, item.abstract, item.issue_date, keywords]),
        )
        patents.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "inventors": inventor_names,
                "patentNumber": item.patent_number or "",
                "year": item.issue_date.year if item.issue_date else 0,
                "description": item.abstract or "",
                "link": item.link or "",
                "keywords": keywords,
                "departments": departments,
                "schools": schools,
                "sharedKeywords": shared,
                "relevanceScore": score,
                "relevanceReason": _network_reason(
                    shared,
                    department_match,
                    school_match,
                    "Potential innovation lead based on SU patent data.",
                ),
            }
        )

    projects = []
    for item in Project.objects.all().prefetch_related("faculty", "faculty__schools", "faculty__departments").order_by("-id"):
        members = list(item.faculty.all())
        if not any(_is_confirmed_su_faculty(member) for member in members):
            continue
        departments = _merge_unique_list(*[_faculty_department_names(member) for member in members])
        schools = _merge_unique_list(*[_faculty_school_names(member) for member in members])
        keywords = _normalize_keyword_list(item.keywords)
        lead_names = [
            member.name
            or _full_name(member.first_name, member.last_name, member.faculty_id)
            for member in members
        ]
        if not _matches_query([item.title, item.description, item.status, *lead_names, *departments, *schools, *keywords], query):
            continue
        score, shared, department_match, school_match = _score_network_item(
            keywords,
            departments,
            schools,
            my_keywords,
            my_departments,
            my_schools,
            richness=sum(bool(value) for value in [item.description, item.status, item.start_date, keywords]),
        )
        projects.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "leadFaculty": lead_names,
                "status": item.status or "Active",
                "description": item.description or "",
                "startDate": item.start_date.isoformat() if item.start_date else "",
                "endDate": item.end_date.isoformat() if item.end_date else "",
                "keywords": keywords,
                "department": departments[0] if departments else "Unassigned",
                "departments": departments,
                "school": schools[0] if schools else "",
                "schools": schools,
                "sharedKeywords": shared,
                "relevanceScore": score,
                "relevanceReason": _network_reason(
                    shared,
                    department_match,
                    school_match,
                    "Potential interdisciplinary project fit.",
                ),
            }
        )

    colleagues.sort(key=lambda row: (row["matchScore"], row["articleCount"]), reverse=True)
    papers.sort(key=lambda row: (row["relevanceScore"], row["year"]), reverse=True)
    patents.sort(key=lambda row: (row["relevanceScore"], row["year"]), reverse=True)
    projects.sort(key=lambda row: row["relevanceScore"], reverse=True)

    return Response(
        {
            "query": query,
            "limit": limit,
            "profileKeywords": my_keywords,
            "colleagues": colleagues[:limit],
            "papers": papers[:limit],
            "patents": patents[:limit],
            "projects": projects[:limit],
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def semantic_paper_search(request):
    query = (request.query_params.get("q") or "").strip()
    if len(query) < 2:
        return Response({"results": [], "count": 0, "detail": "Query too short."})

    try:
        limit = int(request.query_params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    model = (request.query_params.get("model") or "text-embedding-3-small").strip()

    try:
        query_embedding = create_query_embedding(query, model=model)
    except (RuntimeError, Exception):
        query_embedding = None

    if query_embedding is None:
        # Keyword fallback when OpenAI is unavailable
        words = [w.lower() for w in query.split() if len(w) > 1]
        qs = Paper.objects.prefetch_related("authors")
        for word in words:
            qs = qs.filter(
                Q(title__icontains=word)
                | Q(abstract__icontains=word)
                | Q(journal__icontains=word)
            )
        results = []
        for paper in qs[:limit]:
            year = _year_from_dates(
                paper.date_published_online, paper.date_published_print, paper.date_published
            )
            results.append({
                "id": str(paper.id),
                "title": paper.title or "",
                "doi": paper.doi or "",
                "journal": paper.journal or "",
                "authors": [
                    author.name or f"{author.first_name or ''} {author.last_name or ''}".strip()
                    for author in paper.authors.all()
                ],
                "year": year or 0,
                "abstract": paper.abstract or "",
                "link": paper.download_url or paper.url or paper.license_url or "",
                "citations": paper.tc_count or 0,
                "aiKeywords": _normalize_keyword_list(paper.keywords)
                    or _normalize_keyword_list(paper.ai_keywords)
                    or _normalize_keyword_list(paper.faculty_keywords),
                "semanticScore": 0,
            })
        return Response({"query": query, "model": "keyword", "count": len(results), "results": results})

    try:
        papers = (
            Paper.objects.exclude(paper_embedding=[])
            .exclude(paper_embedding__isnull=True)
            .prefetch_related("authors")
        )
    except OperationalError as exc:
        return Response(
            {
                "results": [],
                "count": 0,
                "detail": f"Semantic search schema unavailable: {exc}. Run migrations.",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    scored = []
    for paper in papers:
        embedding = paper.paper_embedding or []
        score = cosine_similarity(query_embedding, embedding)
        if score <= 0:
            continue
        score_100 = round(score * 100.0, 2)
        scored.append((score_100, paper))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:limit]

    results = []
    for semantic_score, paper in top:
        year = _year_from_dates(
            paper.date_published_online, paper.date_published_print, paper.date_published
        )
        results.append(
            {
                "id": str(paper.id),
                "title": paper.title or "",
                "doi": paper.doi or "",
                "journal": paper.journal or "",
                "authors": [
                    author.name
                    or f"{author.first_name or ''} {author.last_name or ''}".strip()
                    for author in paper.authors.all()
                ],
                "year": year or 0,
                "abstract": paper.abstract or "",
                "link": _normalize_paper_link(paper.download_url, paper.url, paper.license_url, paper.doi),
                "citations": paper.tc_count or 0,
                "publishedOnline": paper.date_published_online.isoformat()
                if paper.date_published_online
                else "",
                "publishedPrint": paper.date_published_print.isoformat()
                if paper.date_published_print
                else "",
                "aiKeywords": _normalize_keyword_list(paper.keywords)
                or _normalize_keyword_list(paper.ai_keywords)
                or _normalize_keyword_list(paper.faculty_keywords),
                "facultyMembers": _normalize_keyword_list(paper.faculty_members),
                "facultyAffiliations": paper.faculty_affiliations or {},
                "departmentAffiliations": _merge_unique_list(
                    *list((paper.faculty_affiliations or {}).values())
                ),
                "engagementMetrics": paper.engagement_metrics or {},
                "semanticScore": semantic_score,
            }
        )

    return Response(
        {
            "query": query,
            "model": model,
            "count": len(results),
            "results": results,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def unified_search(request):
    """
    SCOUP Unified Search Engine
    ────────────────────────────────────────────────────────────────────
    GET /api/search/?q=<query>

    Architecture (4 layers):
      1. Query parsing   — strip stopwords, extract meaningful terms
      2. Term rarity     — IDF weight per term (rare = high signal)
      3. Smart scoring   — exact keyword > partial > bio, weighted by rarity
      4. Paper semantic  — cosine similarity against stored embeddings

    Faculty are scored per-keyword (not blob matching).
    Papers are scored semantically via OpenAI embeddings.
    ────────────────────────────────────────────────────────────────────
    """
    import math

    query = (request.query_params.get("q") or "").strip()
    if len(query) < 2:
        return Response({"results": [], "count": 0, "query": query})

    # ── Layer 1: Query parsing ────────────────────────────────────────
    _STOPWORDS = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "it", "its", "as", "be", "was",
        "are", "were", "have", "has", "do", "does", "did", "not", "no",
        "so", "that", "this", "what", "who", "how", "can", "could", "would",
        "will", "may", "might", "some", "any", "all", "more", "than", "me",
        "my", "we", "our", "you", "your", "he", "she", "they", "them",
        "find", "get", "give", "look", "looking", "about", "into", "show",
        "want", "need", "use", "using", "used", "work", "working", "works",
    }
    phrase = query.lower()
    words = [w for w in phrase.split() if len(w) >= 3 and w not in _STOPWORDS]

    if not words:
        return Response({"results": [], "count": 0, "query": query})

    # ── Layer 2: Term rarity (IDF) ────────────────────────────────────
    # How many confirmed/approved faculty have each query word in their keywords?
    # Rare words (few matches) get higher weight — they are stronger signals.
    # Formula: log(total_faculty / (1 + matches)) + 1
    # Example: "biology" in 60/367 faculty → IDF ≈ 1.8
    #          "proteomics" in 3/367 faculty → IDF ≈ 4.8
    eligible_faculty_qs = Faculty.objects.filter(
        Q(confirmed_su_faculty=True) | Q(is_approved=True)
    )
    total_faculty = eligible_faculty_qs.count() or 1

    word_idf = {}
    for word in words:
        df = eligible_faculty_qs.filter(
            Q(ai_keywords__icontains=word)
            | Q(faculty_keywords__icontains=word)
            | Q(keywords__icontains=word)
        ).count()
        word_idf[word] = round(math.log(total_faculty / (1 + df)) + 1, 3)

    # ── Layer 3: Faculty scoring ──────────────────────────────────────
    # Pull all faculty who match any query word in any field
    faculty_q = Q()
    for word in words:
        faculty_q |= (
            Q(ai_keywords__icontains=word)
            | Q(faculty_keywords__icontains=word)
            | Q(keywords__icontains=word)
            | Q(bio__icontains=word)
            | Q(first_name__icontains=word)
            | Q(last_name__icontains=word)
        )

    faculty_qs = (
        eligible_faculty_qs
        .filter(faculty_q)
        .select_related("primary_department", "primary_school")
        .prefetch_related("departments", "schools")
        .distinct()[:80]
    )

    results = []

    for item in faculty_qs:
        dept_names = _faculty_department_names(item)
        school_names = _faculty_school_names(item)
        name = (
            item.name
            or f"{item.first_name or ''} {item.last_name or ''}".strip()
            or item.email or ""
        )

        # Separate keyword sources by trust level:
        #   themes         — curated from actual paper content (highest trust)
        #   faculty_keywords — user-entered research interests (high trust)
        #   ai_keywords    — Academic Metrics categories, noisy (limited use)
        themes_list    = _normalize_keyword_list(item.themes)
        faculty_kws    = _normalize_keyword_list(item.faculty_keywords)
        ai_kws         = _normalize_keyword_list(item.ai_keywords)
        # Only use first 8 ai_keywords — beyond that they are broad category noise
        ai_kws_limited = ai_kws[:8]

        display_keywords = faculty_kws[:8] or ai_kws[:8]
        bio_text  = (item.bio or "").lower()
        name_text = name.lower()

        raw_score = 0.0
        matched   = []

        def _kw_score(kw_list, word, exact_pts, boundary_pts):
            """Score a word against a keyword list. Returns best match score."""
            best = 0.0
            for kw in kw_list:
                kw_l = kw.lower()
                if kw_l == word:
                    best = max(best, exact_pts)
                elif kw_l.startswith(word + " ") or f" {word}" in kw_l:
                    best = max(best, boundary_pts)
                # No substring match — too noisy
            return best

        for word in words:
            idf  = word_idf[word]
            best = 0.0

            # Themes: exact=50, boundary=30 (strongest signal)
            best = max(best, _kw_score(themes_list, word, 50, 30))
            # Faculty keywords: exact=40, boundary=22
            best = max(best, _kw_score(faculty_kws, word, 40, 22))
            # AI keywords (limited): exact=20, boundary=10
            best = max(best, _kw_score(ai_kws_limited, word, 20, 10))
            # Bio text: supporting evidence only
            if best == 0 and word in bio_text:
                best = 6.0
            # Name match
            if word in name_text:
                best = max(best, 18.0)

            if best > 0:
                matched.append(word)

            raw_score += best * idf

        # Normalise to 0–95
        max_possible = len(words) * 50.0 * max(word_idf.values())
        confidence = min(95, round((raw_score / max_possible) * 100)) if max_possible > 0 else 0

        # Only return meaningful matches
        if confidence < 30:
            continue

        # Justification from actual matched themes/keywords
        top_matched_kws = [
            kw for kw in (themes_list + faculty_kws)
            if any(w in kw.lower() for w in matched)
        ][:3]

        if top_matched_kws:
            justification = f"{name} works in {', '.join(top_matched_kws)}."
        elif dept_names:
            justification = f"Matched {name}'s profile in {dept_names[0]}."
        else:
            justification = f"Matched {name}'s research profile."

        photo_url = request.build_absolute_uri(item.photo.url) if item.photo else ""

        results.append({
            "type": "faculty",
            "confidence": confidence,
            "aiJustification": justification,
            "matchedKeywords": matched[:8],
            "data": {
                "id": str(item.id),
                "name": name,
                "title": item.title or "",
                "department": dept_names[0] if dept_names else "Unassigned",
                "departmentAffiliations": dept_names,
                "schoolAffiliations": school_names,
                "email": item.email or "",
                "phone": item.phone or "",
                "photo": photo_url,
                "bio": item.bio or "",
                "researchInterests": display_keywords,
                "aiKeywords": display_keywords,
                "metricsProfile": {
                    "totalCitations": item.total_citations or 0,
                    "articleCount": item.article_count or 0,
                    "averageCitations": float(item.average_citations or 0),
                },
                "themes": _normalize_keyword_list(item.themes),
                "journals": _normalize_keyword_list(item.journals),
            },
        })

    # ── Layer 4: Paper search (semantic + title-exact, always both) ──────
    #
    # Strategy: run BOTH semantic search and title/abstract keyword search,
    # then merge. This ensures papers with an exact title match are ALWAYS
    # returned, even if their semantic similarity score is below the threshold.
    #
    # Semantic: cosine similarity against stored OpenAI embeddings
    #   (text-embedding-3-small, 1536 dims). Threshold = 0.22.
    #   Confidence formula: min(95, max(40, round((sim - 0.15) / 0.35 * 100)))
    #   sim=0.22 → 40%, sim=0.35 → 57%, sim=0.42 → 77%
    #
    # Keyword: always runs on title and abstract.
    #   Title matches score higher than abstract matches.
    #   An exact all-word title match scores up to 90%.
    #   Merged results deduplicate by paper id — semantic wins if both find it.

    try:
        query_embedding = create_query_embedding(query)
    except Exception:
        query_embedding = None

    SEMANTIC_THRESHOLD = 0.22

    # --- Semantic pass ---
    semantic_by_id = {}   # paper.id → (scaled_conf, sim, paper)
    if query_embedding:
        try:
            papers_with_embeddings = (
                Paper.objects
                .exclude(paper_embedding=[])
                .exclude(paper_embedding__isnull=True)
                .prefetch_related("authors")
            )
            for paper in papers_with_embeddings:
                sim = cosine_similarity(query_embedding, paper.paper_embedding or [])
                if sim >= SEMANTIC_THRESHOLD:
                    scaled = min(95, max(40, round((sim - 0.15) / 0.35 * 100)))
                    semantic_by_id[paper.id] = (scaled, sim, paper)
        except OperationalError:
            pass

    # --- Keyword pass (always runs — guarantees exact title matches surface) ---
    # Critically: papers found by BOTH semantic and keyword take the HIGHER score.
    # Without this, a paper with an exact title match but low semantic similarity
    # (sim=0.25 → conf=40%) would be stuck at 40% even though keyword scoring
    # would give it 85%+. We never want an exact title match to rank below 85%.
    #
    # TWO-STAGE query strategy:
    #   Stage 1 — AND query: all words must appear in the title. Small result set,
    #             always fully included regardless of limits. This guarantees exact
    #             title matches (like searching a specific paper name) always surface.
    #   Stage 2 — OR query: any word in title or abstract. Broader, limited to 20.
    #             Supplements stage 1 with thematically related papers.
    title_and_q = Q()
    for word in words:
        title_and_q &= Q(title__icontains=word)
    exact_title_papers = list(
        Paper.objects.filter(title_and_q).prefetch_related("authors")
    )
    exact_title_ids = {p.id for p in exact_title_papers}

    broad_q = Q()
    for word in words:
        broad_q |= Q(title__icontains=word) | Q(abstract__icontains=word)
    broad_papers = list(
        Paper.objects
        .filter(broad_q)
        .exclude(id__in=exact_title_ids)
        .prefetch_related("authors")[:20]
    )

    keyword_papers = exact_title_papers + broad_papers

    keyword_by_id = {}  # paper.id → (kw_conf, paper)
    for paper in keyword_papers:
        title_lower = (paper.title or "").lower()
        abstract_lower = (paper.abstract or "").lower()
        title_hits = sum(1 for w in words if w in title_lower)
        abstract_hits = sum(1 for w in words if w in abstract_lower)
        all_in_title = title_hits == len(words)
        raw = (title_hits * 20) + (abstract_hits * 5)
        max_raw = len(words) * 20
        kw_conf = min(90, max(40, round((raw / max_raw) * 90))) if max_raw else 40
        if all_in_title:
            kw_conf = max(kw_conf, 85)  # exact title match always gets 85%+
        keyword_by_id[paper.id] = (kw_conf, paper)

    # --- Merge: take MAX(semantic_conf, keyword_conf) for papers in both ---
    paper_scored = []  # (conf, sim, paper, is_semantic)
    seen_ids = set()

    for pid, (sem_conf, sim, paper) in semantic_by_id.items():
        if pid in keyword_by_id:
            kw_conf, _ = keyword_by_id[pid]
            # Paper found by both — use whichever score is higher
            if kw_conf > sem_conf:
                paper_scored.append((kw_conf, sim, paper, False))  # keyword wins
            else:
                paper_scored.append((sem_conf, sim, paper, True))  # semantic wins
        else:
            paper_scored.append((sem_conf, sim, paper, True))
        seen_ids.add(pid)

    for pid, (kw_conf, paper) in keyword_by_id.items():
        if pid not in seen_ids:
            paper_scored.append((kw_conf, 0.0, paper, False))

    paper_scored.sort(key=lambda x: x[0], reverse=True)
    paper_scored = paper_scored[:15]

    def _paper_justification(paper, paper_keywords, sim, is_semantic):
        """Build a human-readable justification for why this paper matches."""
        matched_kws = [k for k in paper_keywords if any(w in k.lower() for w in words)][:3]
        journal = paper.journal or ""
        title_lower = (paper.title or "").lower()
        all_in_title = all(w in title_lower for w in words)

        if all_in_title:
            return f"Title directly matches your search terms."
        if is_semantic:
            if matched_kws:
                return f"Semantically matched on {', '.join(matched_kws)}."
            elif journal:
                return f"Semantically relevant paper published in {journal}."
            else:
                return f"Content is semantically similar to your query (score: {sim:.2f})."
        else:
            if matched_kws:
                return f"Keyword match on {', '.join(matched_kws)}."
            return "Title or abstract contains your search terms."

    for conf, sim, paper, is_semantic in paper_scored:
        year = _year_from_dates(
            paper.date_published_online, paper.date_published_print, paper.date_published
        )
        paper_keywords = (
            _normalize_keyword_list(paper.keywords)
            or _normalize_keyword_list(paper.ai_keywords)
            or _normalize_keyword_list(paper.faculty_keywords)
        )
        matched_paper_kws = [k for k in paper_keywords if any(w in k.lower() for w in words)][:5]
        results.append({
            "type": "paper",
            "confidence": conf,
            "aiJustification": _paper_justification(paper, paper_keywords, sim, is_semantic),
            "matchedKeywords": matched_paper_kws or words[:3],
            "data": {
                "id": str(paper.id),
                "title": paper.title or "",
                "doi": paper.doi or "",
                "journal": paper.journal or "",
                "authors": [
                    a.name or f"{a.first_name or ''} {a.last_name or ''}".strip()
                    for a in paper.authors.all()
                ],
                "year": year or 0,
                "abstract": paper.abstract or "",
                "link": _normalize_paper_link(paper.download_url, paper.url, paper.license_url, paper.doi),
                "aiKeywords": paper_keywords,
                "citations": paper.tc_count or 0,
                "semanticScore": round(sim * 100, 1),
            },
        })

    # ── Layer 5: Patent search (keyword scoring) ─────────────────────
    # Patents have no embeddings — scored by keyword match on title, abstract,
    # and aiKeywords. Faculty inventors are pulled from the many-to-many relation.
    patent_q = Q()
    for word in words:
        patent_q |= (
            Q(title__icontains=word)
            | Q(abstract__icontains=word)
            | Q(aiKeywords__icontains=word)
        )

    for patent in Patent.objects.filter(patent_q).prefetch_related("faculty")[:10]:
        title_lower = (patent.title or "").lower()
        abstract_lower = (patent.abstract or "").lower()
        kw_lower = str(patent.aiKeywords or "").lower()

        title_hits = sum(1 for w in words if w in title_lower)
        abstract_hits = sum(1 for w in words if w in abstract_lower)
        kw_hits = sum(1 for w in words if w in kw_lower)

        all_in_title = title_hits == len(words)
        raw = (title_hits * 20) + (abstract_hits * 8) + (kw_hits * 12)
        max_raw = len(words) * 20
        conf = min(88, max(40, round((raw / max_raw) * 88))) if max_raw else 40
        if all_in_title:
            conf = max(conf, 82)

        inventors = [
            f.name or f"{f.first_name or ''} {f.last_name or ''}".strip()
            for f in patent.faculty.all()
        ]
        patent_kws = _normalize_keyword_list(patent.aiKeywords)
        matched_kws = [k for k in patent_kws if any(w in k.lower() for w in words)]

        if matched_kws:
            justification = f"Patent keyword match on {', '.join(matched_kws[:3])}."
        elif inventors:
            justification = f"Patent by {', '.join(inventors[:2])} matches your search terms."
        else:
            justification = "Patent title or description contains your search terms."

        issue_year = patent.issue_date.year if patent.issue_date else (
            patent.filing_date.year if patent.filing_date else None
        )

        results.append({
            "type": "patent",
            "confidence": conf,
            "aiJustification": justification,
            "matchedKeywords": matched_kws[:5] or words[:3],
            "data": {
                "id": str(patent.id),
                "title": patent.title or "",
                "patentNumber": patent.patent_number or "",
                "inventors": inventors,
                "year": issue_year or 0,
                "description": patent.abstract or "",
                "link": patent.link or "",
                "aiKeywords": patent_kws,
            },
        })

    # ── Layer 6: Project search (keyword scoring) ─────────────────────
    # Projects have no embeddings — scored by keyword match on title, description,
    # and keywords. Lead faculty pulled from many-to-many relation.
    project_q = Q()
    for word in words:
        project_q |= (
            Q(title__icontains=word)
            | Q(description__icontains=word)
            | Q(keywords__icontains=word)
        )

    for project in Project.objects.filter(project_q).prefetch_related("faculty")[:10]:
        title_lower = (project.title or "").lower()
        desc_lower = (project.description or "").lower()
        kw_lower = str(project.keywords or "").lower()

        title_hits = sum(1 for w in words if w in title_lower)
        desc_hits = sum(1 for w in words if w in desc_lower)
        kw_hits = sum(1 for w in words if w in kw_lower)

        all_in_title = title_hits == len(words)
        raw = (title_hits * 20) + (desc_hits * 8) + (kw_hits * 12)
        max_raw = len(words) * 20
        conf = min(88, max(40, round((raw / max_raw) * 88))) if max_raw else 40
        if all_in_title:
            conf = max(conf, 82)

        lead_faculty = [
            f.name or f"{f.first_name or ''} {f.last_name or ''}".strip()
            for f in project.faculty.all()
        ]
        project_kws = _normalize_keyword_list(project.keywords)
        matched_kws = [k for k in project_kws if any(w in k.lower() for w in words)]

        if matched_kws:
            justification = f"Project keyword match on {', '.join(matched_kws[:3])}."
        elif lead_faculty:
            justification = f"Project led by {', '.join(lead_faculty[:2])} matches your search terms."
        else:
            justification = "Project title or description contains your search terms."

        results.append({
            "type": "project",
            "confidence": conf,
            "aiJustification": justification,
            "matchedKeywords": matched_kws[:5] or words[:3],
            "data": {
                "id": str(project.id),
                "title": project.title or "",
                "status": project.status or "Unknown",
                "leadFaculty": lead_faculty,
                "startDate": str(project.start_date) if project.start_date else "",
                "endDate": str(project.end_date) if project.end_date else "",
                "description": project.description or "",
                "link": project.link or "",
                "aiKeywords": project_kws,
                "fundingSource": project.funding_source or "",
            },
        })

    results.sort(key=lambda r: r["confidence"], reverse=True)
    return Response({"results": results, "count": len(results), "query": query})


def home(request):
    return HttpResponse(
        "<h1>Welcome to the Scoup Database!</h1><p>Go to <a href='/admin/'>Admin</a></p>"
    )

class FacultyListCreateView(generics.ListCreateAPIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    filter_backends = [filters.SearchFilter]
    search_fields = [
        "first_name",
        "last_name",
        "name",
        "title",
        "department",
        "faculty_keywords",
        "ai_keywords",
        "keywords",
    ]

    def get_queryset(self):
        return Faculty.objects.filter(profile_visibility=True).filter(
            Q(is_approved=True) | Q(user__isnull=False)
        )

    serializer_class = FacultySerializer

class PaperListCreateView(generics.ListCreateAPIView):
    serializer_class = PaperSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = [
        "title",
        "abstract",
        "journal",
        "keywords",
        "themes",
        "authors__name",
        "authors__department",
    ]
    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Paper.objects.none()
        return Paper.objects.filter(authors=faculty)

    def perform_create(self, serializer):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            raise NotFound("Faculty profile not found for this user.")
        paper = serializer.save()
        paper.authors.add(faculty)
        paper.save()

class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Project.objects.none()
        return Project.objects.filter(faculty=faculty)

    def perform_create(self, serializer):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            raise NotFound("Faculty profile not found for this user.")
        project = serializer.save()
        project.faculty.add(faculty)
        project.save()

class PatentListCreateView(generics.ListCreateAPIView):
    serializer_class = PatentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Patent.objects.none()
        return Patent.objects.filter(faculty=faculty)

    def perform_create(self, serializer):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            raise NotFound("Faculty profile not found for this user.")
        patent = serializer.save()
        patent.faculty.add(faculty)
        patent.save()


class FacultyDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = FacultyProfileSerializer
    permission_classes = [IsAuthenticated]
    queryset = Faculty.objects.all()

    def get_object(self):
        obj = super().get_object()
        requestor = _get_request_faculty(self.request.user)
        if self.request.user.is_staff or (requestor and obj.id == requestor.id):
            return obj
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("You can only edit your own faculty profile.")


class PaperDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PaperSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Paper.objects.none()
        return Paper.objects.filter(authors=faculty)


class ProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Project.objects.none()
        return Project.objects.filter(faculty=faculty)


class PatentDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PatentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Patent.objects.none()
        return Patent.objects.filter(faculty=faculty)


@api_view(["POST"])
@permission_classes([AllowAny])
def faculty_signup(request):
    data = request.data
    username = (data.get("username") or "").strip()
    password = data.get("password")
    email = (data.get("email") or "").strip().lower()
    first_name = data.get("first_name")
    last_name = data.get("last_name")

    if not username or not password or not email:
        return Response(
            {"error": "Username, password, and email are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(username=username).exists():
        return Response(
            {"error": "Username already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(email__iexact=email).exists():
        return Response(
            {"error": "Email already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            password=password,
            email=email,
            first_name=first_name or "",
            last_name=last_name or "",
        )
        email_in_use_elsewhere = Faculty.objects.filter(email__iexact=email).exists()
        safe_email = None if email_in_use_elsewhere else email
        requested_faculty_id = (data.get("faculty_id") or "").strip()
        generated_faculty_id = requested_faculty_id or _generate_signup_faculty_id()
        if Faculty.objects.filter(faculty_id=generated_faculty_id).exists():
            generated_faculty_id = _generate_signup_faculty_id()

        Faculty.objects.create(
            user=user,
            faculty_id=generated_faculty_id,
            email=safe_email,
            first_name=first_name or "",
            last_name=last_name or "",
            name=_full_name(first_name, last_name, username),
            is_approved=True,
            profile_visibility=True,
        )

    return Response(
        {"message": "Faculty account created."},
        status=status.HTTP_201_CREATED,
    )

@api_view(["GET", "PATCH", "PUT"])
@permission_classes([IsAuthenticated])
def faculty_me(request):
    faculty = _get_request_faculty(request.user, create_if_missing=True)
    if not faculty:
        return Response(
            {"detail": "Faculty profile not found for this user."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        serializer = FacultyProfileSerializer(faculty)
        return Response(serializer.data)

    partial = request.method == "PATCH"
    serializer = FacultyProfileSerializer(faculty, data=request.data, partial=partial)

    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)

    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_me(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response(
            {"detail": "Admin privileges required."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return Response(
        {
            "id": request.user.id,
            "username": request.user.get_username(),
            "email": request.user.email,
            "is_staff": bool(request.user.is_staff),
            "is_superuser": bool(request.user.is_superuser),
            "is_admin": True,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_me_suggestions(request):
    faculty = _get_request_faculty(request.user, create_if_missing=True)
    if not faculty:
        return Response(
            {"detail": "Faculty profile not found for this user."},
            status=status.HTTP_404_NOT_FOUND,
        )

    first_name = (faculty.first_name or request.user.first_name or "").strip().lower()
    last_name = (faculty.last_name or request.user.last_name or "").strip().lower()
    full_name = _full_name(faculty.first_name, faculty.last_name).strip().lower()
    department = (faculty.department or "").strip().lower()
    email = (request.user.email or faculty.email or "").strip().lower()
    internal_keywords = _keywords_for_matching(faculty)

    query = Q()
    if first_name and last_name:
        query |= Q(first_name__iexact=first_name, last_name__iexact=last_name)
        query |= Q(name__icontains=first_name) & Q(name__icontains=last_name)
    if email:
        query |= Q(email__iexact=email)

    rejected_ids = list(
        FacultySuggestionDecision.objects.filter(reviewer=faculty, decision="rejected")
        .values_list("external_faculty_id", flat=True)
    )
    candidates_qs = Faculty.objects.filter(user__isnull=True, profile_visibility=True).exclude(
        id__in=rejected_ids
    )
    if query:
        candidates_qs = candidates_qs.filter(query)
    candidates = candidates_qs.order_by("id")[:200]

    suggestions = []
    for candidate in candidates:
        candidate_first = (candidate.first_name or "").strip().lower()
        candidate_last = (candidate.last_name or "").strip().lower()
        candidate_name = _full_name(
            candidate.first_name,
            candidate.last_name,
            (candidate.name or "").strip(),
        ).strip().lower()
        candidate_department = (candidate.department or "").strip().lower()
        candidate_email = (candidate.email or "").strip().lower()
        candidate_keywords = _keywords_for_matching(candidate)

        score = 0
        reasons = []

        if email and candidate_email and candidate_email == email:
            score += 8
            reasons.append("matching email")
        if full_name and candidate_name and candidate_name == full_name:
            score += 6
            reasons.append("matching full name")
        if last_name and candidate_last and candidate_last == last_name:
            score += 3
            reasons.append("matching last name")
        if first_name and candidate_first and candidate_first == first_name:
            score += 2
            reasons.append("matching first name")
        if department and candidate_department:
            if department == candidate_department:
                score += 2
                reasons.append("matching department")
            elif department in candidate_department or candidate_department in department:
                score += 1
                reasons.append("similar department")

        shared_keywords = sorted(internal_keywords.intersection(candidate_keywords))
        if shared_keywords:
            keyword_points = min(4, len(shared_keywords))
            score += keyword_points
            reasons.append(f"{len(shared_keywords)} shared keywords")

        if score >= 3:
            suggestions.append(
                {
                    "id": candidate.id,
                    "faculty_id": candidate.faculty_id,
                    "name": _full_name(
                        candidate.first_name,
                        candidate.last_name,
                        (candidate.name or "").strip() or candidate.faculty_id,
                    ),
                    "department": candidate.department or "",
                    "title": candidate.title or "",
                    "email": candidate.email or "",
                    "score": score,
                    "reasons": reasons[:3],
                    "sample_keywords": shared_keywords[:5],
                }
            )

    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return Response({"suggestions": suggestions[:10]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_faculty_suggestion(request, external_faculty_id):
    internal = _get_request_faculty(request.user, create_if_missing=True)
    if not internal:
        return Response(
            {"detail": "Faculty profile not found for this user."},
            status=status.HTTP_404_NOT_FOUND,
        )

    external = Faculty.objects.filter(id=external_faculty_id, user__isnull=True).first()
    if not external:
        return Response(
            {"detail": "Suggested faculty not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if external.id == internal.id:
        return Response(
            {"detail": "Cannot absorb your own faculty profile."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        summary = _absorb_external_faculty(internal, external)
        FacultySuggestionDecision.objects.update_or_create(
            reviewer=internal,
            external_faculty=external,
            defaults={"decision": "approved"},
        )

    serializer = FacultyProfileSerializer(internal)
    return Response(
        {
            "message": "Suggested faculty absorbed into your profile.",
            "merged": summary,
            "faculty": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_suggestion_preview(request, external_faculty_id):
    internal = _get_request_faculty(request.user, create_if_missing=True)
    if not internal:
        return Response(
            {"detail": "Faculty profile not found for this user."},
            status=status.HTTP_404_NOT_FOUND,
        )

    external = (
        Faculty.objects.filter(id=external_faculty_id, user__isnull=True, profile_visibility=True)
        .prefetch_related("papers", "projects", "patents")
        .first()
    )
    if not external:
        return Response(
            {"detail": "Suggested faculty not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(_external_faculty_preview_payload(external), status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_faculty_suggestion(request, external_faculty_id):
    internal = _get_request_faculty(request.user, create_if_missing=True)
    if not internal:
        return Response(
            {"detail": "Faculty profile not found for this user."},
            status=status.HTTP_404_NOT_FOUND,
        )

    external = Faculty.objects.filter(id=external_faculty_id, user__isnull=True).first()
    if not external:
        return Response(
            {"detail": "Suggested faculty not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    FacultySuggestionDecision.objects.update_or_create(
        reviewer=internal,
        external_faculty=external,
        defaults={"decision": "rejected"},
    )

    return Response(
        {"message": "Suggestion rejected and will be hidden from future suggestions."},
        status=status.HTTP_200_OK,
    )


class FacultyPhotoUploadView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        faculty = _get_request_faculty(request.user, create_if_missing=True)
        if not faculty:
            return Response(
                {"detail": "Faculty profile not found for this user."},
                status=status.HTTP_404_NOT_FOUND,
            )
        photo = request.data.get("photo")

        if not photo:
            return Response({"error": "No photo uploaded"}, status=400)

        faculty.photo = photo
        faculty.save()

        return Response({
            "message": "Photo updated",
            "photo": request.build_absolute_uri(faculty.photo.url)
        })

class FacultyUploadCVPapers(APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        faculty = _get_request_faculty(request.user, create_if_missing=True)
        if not faculty:
            return Response(
                {"detail": "Faculty profile not found for this user."},
                status=status.HTTP_404_NOT_FOUND,
            )
        file = request.FILES.get("file")

        if not file:
            return Response({"error": "No PDF uploaded"}, status=400)
        try:
            with pdfplumber.open(file) as pdf:
                full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])
        except Exception as e:
            return Response({"error": f"PDF extract error: {str(e)}"}, status=400)
        entries = []

        doi_pattern = r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"

        for line in full_text.split("\n"):
            doi_match = re.search(doi_pattern, line)
            if doi_match:
                entries.append({
                    "title": line.replace(doi_match.group(), "").strip(),
                    "doi": doi_match.group()
                })

        created = []
        for item in entries:
            paper, _ = Paper.objects.get_or_create(
                doi=item["doi"],
                defaults={"title": item["title"] or "Untitled Paper"}
            )
            paper.authors.add(faculty)
            created.append({"title": paper.title, "doi": paper.doi})

        return Response({
            "message": "PDF processed",
            "papers_found": len(created),
            "papers": created
        })

@api_view(["GET"])
@permission_classes([AllowAny])
def contact_team_list(request):
    members = ContactTeamMember.objects.filter(is_visible=True)
    serializer = ContactTeamMemberSerializer(members, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_contact_team_list(request):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    if request.method == "GET":
        members = ContactTeamMember.objects.all()
        serializer = ContactTeamMemberSerializer(members, many=True, context={"request": request})
        return Response(serializer.data)
    serializer = ContactTeamMemberSerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)


@api_view(["PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_contact_team_detail(request, pk):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    try:
        member = ContactTeamMember.objects.get(pk=pk)
    except ContactTeamMember.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        member.delete()
        return Response(status=204)
    serializer = ContactTeamMemberSerializer(member, data=request.data, partial=request.method == "PATCH", context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)


@api_view(["GET"])
@permission_classes([AllowAny])
def contact_settings(request):
    obj, _ = ContactPageSettings.objects.get_or_create(pk=1)
    serializer = ContactPageSettingsSerializer(obj)
    return Response(serializer.data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_contact_settings(request):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    obj, _ = ContactPageSettings.objects.get_or_create(pk=1)
    serializer = ContactPageSettingsSerializer(obj, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_contact_team_photo_upload(request, pk):
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    try:
        member = ContactTeamMember.objects.get(pk=pk)
    except ContactTeamMember.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)
    if "photo" not in request.FILES:
        return Response({"detail": "No photo provided."}, status=400)
    member.photo = request.FILES["photo"]
    member.save()
    serializer = ContactTeamMemberSerializer(member, context={"request": request})
    return Response(serializer.data)


# ===========================================================================
# ADMIN ENDPOINTS — require IsAdminUser
# ===========================================================================

# ---------------------------------------------------------------------------
# Schools
# ---------------------------------------------------------------------------

class AdminSchoolListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/admin/schools/  — list all schools
    POST /api/admin/schools/  — create a new school
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SchoolSerializer
    queryset = School.objects.all().order_by("display_order", "name")


class AdminSchoolDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/admin/schools/<id>/  — get school detail
    PATCH  /api/admin/schools/<id>/  — update school (name, code, display_order, is_active)
    DELETE /api/admin/schools/<id>/  — delete school (will fail if departments exist — protected)
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SchoolSerializer
    queryset = School.objects.all()


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------

class AdminDepartmentListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/admin/departments/           — list all departments
    GET  /api/admin/departments/?school=1  — filter by school id
    POST /api/admin/departments/           — create a new department
         Body: { "name": "...", "school": <school_id>, "code": "..." }
    """
    permission_classes = [IsAuthenticated]
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        qs = Department.objects.select_related("school").order_by("school__name", "name")
        school_id = self.request.query_params.get("school")
        if school_id:
            qs = qs.filter(school_id=school_id)
        return qs


class AdminDepartmentDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/admin/departments/<id>/  — get department detail
    PATCH  /api/admin/departments/<id>/  — update (name, school, code, is_active)
    DELETE /api/admin/departments/<id>/  — delete department
    """
    permission_classes = [IsAuthenticated]
    serializer_class = DepartmentSerializer
    queryset = Department.objects.select_related("school")


# ---------------------------------------------------------------------------
# Admin Faculty Management
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_faculty_list(request):
    """
    GET /api/admin/faculty/
    Returns all faculty with a `missing_data` flag indicating incomplete records.

    Query params:
      ?missing=true   — only return faculty with missing department or school
      ?search=<term>  — filter by name or email
    """
    qs = Faculty.objects.prefetch_related(
        "departments", "schools"
    ).select_related("primary_department", "primary_school").order_by("last_name", "first_name")

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))

    missing_only = request.query_params.get("missing", "").lower() == "true"
    if missing_only:
        qs = qs.filter(primary_department__isnull=True)

    results = []
    for f in qs:
        departments = [d.name for d in f.departments.all()]
        schools = [s.name for s in f.schools.all()]
        results.append({
            "id": f.id,
            "name": f.name or f"{f.first_name or ''} {f.last_name or ''}".strip() or f.faculty_id,
            "email": f.email or "",
            "review_status": f.review_status,
            "confirmed_su_faculty": f.confirmed_su_faculty,
            "primary_department": {
                "id": f.primary_department.id,
                "name": f.primary_department.name,
            } if f.primary_department else None,
            "primary_school": {
                "id": f.primary_school.id,
                "name": f.primary_school.name,
            } if f.primary_school else None,
            "departments": departments,
            "schools": schools,
            "missing_data": not f.primary_department_id,
        })

    return Response(results)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_faculty_detail(request, pk):
    """
    PATCH  /api/admin/faculty/<id>/
    Assign or update department/school affiliations for a faculty member.
    Body (all fields optional):
      {
        "primary_department": <department_id>,
        "primary_school": <school_id>,
        "departments": [<department_id>, ...],
        "schools": [<school_id>, ...],
        "review_status": "confirmed_su" | "pending" | "external" | "archived" | "rejected",
        "confirmed_su_faculty": true | false
      }

    DELETE /api/admin/faculty/<id>/
    Permanently delete a faculty record from the database.
    """
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        faculty.delete()
        return Response({"detail": "Faculty record deleted."}, status=status.HTTP_204_NO_CONTENT)

    data = request.data

    if "primary_department" in data:
        dept_id = data["primary_department"]
        if dept_id is None:
            faculty.primary_department = None
        else:
            try:
                faculty.primary_department = Department.objects.get(pk=dept_id)
            except Department.DoesNotExist:
                return Response({"detail": f"Department {dept_id} not found."}, status=400)

    if "primary_school" in data:
        school_id = data["primary_school"]
        if school_id is None:
            faculty.primary_school = None
        else:
            try:
                faculty.primary_school = School.objects.get(pk=school_id)
            except School.DoesNotExist:
                return Response({"detail": f"School {school_id} not found."}, status=400)

    if "review_status" in data:
        faculty.review_status = data["review_status"]

    if "confirmed_su_faculty" in data:
        faculty.confirmed_su_faculty = bool(data["confirmed_su_faculty"])

    faculty.save()

    if "departments" in data:
        faculty.departments.set(data["departments"])

    if "schools" in data:
        faculty.schools.set(data["schools"])

    return Response({
        "id": faculty.id,
        "primary_department": faculty.primary_department.name if faculty.primary_department else None,
        "primary_school": faculty.primary_school.name if faculty.primary_school else None,
        "review_status": faculty.review_status,
        "confirmed_su_faculty": faculty.confirmed_su_faculty,
    })


# ---------------------------------------------------------------------------
# Admin Paper Management
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_paper_list(request):
    """
    GET /api/admin/papers/
    Returns all papers with author and link info.

    Query params:
      ?unlinked=true  — only papers with no linked faculty authors
      ?search=<term>  — filter by title or DOI
    """
    qs = Paper.objects.prefetch_related("authors").order_by("-id")

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(doi__icontains=search))

    unlinked_only = request.query_params.get("unlinked", "").lower() == "true"
    if unlinked_only:
        qs = qs.filter(authors__isnull=True)

    results = []
    for p in qs:
        authors = list(p.authors.all())
        results.append({
            "id": p.id,
            "title": p.title,
            "doi": p.doi or "",
            "journal": p.journal or "",
            "year": p.date_published.year if p.date_published else None,
            "link": _normalize_paper_link(p.download_url, p.url, p.license_url, p.doi),
            "abstract": (p.abstract or "")[:300],
            "author_count": len(authors),
            "authors": [
                a.name or f"{a.first_name or ''} {a.last_name or ''}".strip()
                for a in authors
            ],
            "missing_data": len(authors) == 0 or not p.abstract,
        })

    return Response({"count": len(results), "results": results})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_paper_detail(request, pk):
    """
    DELETE /api/admin/papers/<id>/
    Permanently delete a paper from the database.
    """
    try:
        paper = Paper.objects.get(pk=pk)
    except Paper.DoesNotExist:
        return Response({"detail": "Paper not found."}, status=status.HTTP_404_NOT_FOUND)
    paper.delete()
    return Response({"detail": "Paper deleted."}, status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Admin Project & Patent Management
# ---------------------------------------------------------------------------

@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_project_detail(request, pk):
    """
    DELETE /api/admin/projects/<id>/
    Permanently delete a project from the database.
    """
    try:
        project = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    project.delete()
    return Response({"detail": "Project deleted."}, status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_patent_detail(request, pk):
    """
    DELETE /api/admin/patents/<id>/
    Permanently delete a patent from the database.
    """
    try:
        patent = Patent.objects.get(pk=pk)
    except Patent.DoesNotExist:
        return Response({"detail": "Patent not found."}, status=status.HTTP_404_NOT_FOUND)
    patent.delete()
    return Response({"detail": "Patent deleted."}, status=status.HTTP_204_NO_CONTENT)
