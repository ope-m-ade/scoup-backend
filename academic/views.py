
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

from .affiliations import (
    extract_departments_from_faculty_affiliations,
    extract_salisbury_departments,
    extract_salisbury_schools,
    extract_schools_from_faculty_affiliations,
    sanitize_department_label,
    sanitize_school_label,
)
from .models import (
    Faculty,
    FacultySuggestionDecision,
    Paper,
    PaperAuthorship,
    Patent,
    Project,
    ContactTeamMember,
    ContactPageSettings,
)
from .serializers import (
    FacultyProfileSerializer,
    FacultySerializer,
    PaperSerializer,
    PatentSerializer,
    ProjectSerializer,
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
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        if clean_value.startswith(("http://", "https://")):
            return clean_value

    clean_doi = str(doi or "").strip()
    if clean_doi:
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


def _legacy_school_names(faculty):
    sanitized = []
    if getattr(faculty, "school", None):
        clean = sanitize_school_label(faculty.school)
        if clean:
            sanitized.append(clean)
    return _merge_unique_list(
        sanitized,
        extract_salisbury_schools(
            _merge_unique_list(
                getattr(faculty, "school_affiliations", []),
                getattr(faculty, "department_affiliations", []),
                [faculty.school] if getattr(faculty, "school", None) else [],
                [faculty.department] if getattr(faculty, "department", None) else [],
            )
        ),
    )


def _legacy_department_names(faculty):
    sanitized = []
    if getattr(faculty, "department", None):
        clean = sanitize_department_label(faculty.department)
        if clean:
            sanitized.append(clean)
    return _merge_unique_list(
        sanitized,
        extract_salisbury_departments(
            _merge_unique_list(
                getattr(faculty, "department_affiliations", []),
                getattr(faculty, "school_affiliations", []),
                [faculty.department] if getattr(faculty, "department", None) else [],
                [faculty.school] if getattr(faculty, "school", None) else [],
            )
        ),
    )


def _has_salisbury_department(faculty):
    return bool(_faculty_department_names(faculty) or _faculty_school_names(faculty))


def _faculty_school_names(faculty):
    names = []
    if getattr(faculty, "primary_school_id", None):
        names.append(faculty.primary_school.name)
    names.extend([school.name for school in faculty.schools.all()])
    return _merge_unique_list(names, _legacy_school_names(faculty))


def _faculty_department_names(faculty):
    names = []
    if getattr(faculty, "primary_department_id", None):
        names.append(faculty.primary_department.name)
    names.extend([department.name for department in faculty.departments.all()])
    return _merge_unique_list(names, _legacy_department_names(faculty))


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
                "department": departments[0] if departments else "",
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
            extract_departments_from_faculty_affiliations(item.faculty_affiliations),
            *[_faculty_department_names(author) for author in authors],
        )
        paper_schools = _merge_unique_list(
            extract_schools_from_faculty_affiliations(item.faculty_affiliations),
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
                "department": departments[0] if departments else "",
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
        raw_affiliation_departments = extract_departments_from_faculty_affiliations(
            item.faculty_affiliations
        )
        raw_affiliation_schools = extract_schools_from_faculty_affiliations(
            item.faculty_affiliations
        )
        departments = _merge_unique_list(
            raw_affiliation_departments,
            *[_faculty_department_names(author) for author in authors],
        )
        schools = _merge_unique_list(
            raw_affiliation_schools,
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
                "department": departments[0] if departments else "",
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
    if first_name:
        query |= Q(first_name__iexact=first_name) | Q(name__icontains=first_name)
    if last_name:
        query |= Q(last_name__iexact=last_name) | Q(name__icontains=last_name)
    if department:
        query |= Q(department__icontains=department)
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
