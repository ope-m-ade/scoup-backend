
import json
import os
import re
import secrets
import uuid

import pdfplumber
import requests as http_requests
from openai import OpenAI
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Prefetch, Q
from django.db.utils import OperationalError
from django.http import HttpResponse
from rest_framework import filters, generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied
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
from .search_engine import run_search


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


# Matches bare DOIs and doi.org URL prefixes
_DOI_URL_PREFIX = re.compile(r"https?://(dx\.)?doi\.org/", re.IGNORECASE)


def _clean_abstract(raw: str) -> str:
    """Strip JATS/HTML tags and normalise whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap ratio to verify a search result matches our paper."""
    a_words = set(re.sub(r"[^a-z0-9 ]", "", a.lower()).split())
    b_words = set(re.sub(r"[^a-z0-9 ]", "", b.lower()).split())
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / max(len(a_words), len(b_words))


def fetch_abstract(title: str, doi: str | None) -> str | None:
    """
    Try to fetch a real abstract for a paper via CrossRef (by DOI) then
    Semantic Scholar and CrossRef title search (with similarity gating).
    Returns the abstract string or None if not found.
    """
    headers = {"User-Agent": "SCOUP/1.0 (mailto:scoupteam@gmail.com)"}

    # 1. CrossRef by DOI — exact, most reliable
    if doi:
        try:
            r = http_requests.get(
                f"https://api.crossref.org/works/{doi}",
                timeout=4, headers=headers,
            )
            if r.ok:
                abstract = _clean_abstract(r.json().get("message", {}).get("abstract", ""))
                if abstract:
                    return abstract
        except Exception:
            pass

    # 2. Semantic Scholar by title — better abstract coverage
    try:
        r = http_requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": title, "fields": "title,abstract", "limit": 3},
            timeout=4,
        )
        if r.ok:
            for item in r.json().get("data", []):
                if item.get("abstract") and _title_similarity(title, item.get("title", "")) >= 0.45:
                    return item["abstract"]
    except Exception:
        pass

    # 3. CrossRef title search — only if we have a DOI to anchor the search
    # (skip for title-only lookups to save time on large CVs)
    if doi:
        try:
            r = http_requests.get(
                "https://api.crossref.org/works",
                params={"query.title": title, "rows": 3, "select": "title,abstract,DOI"},
                timeout=4, headers=headers,
            )
            if r.ok:
                for item in r.json().get("message", {}).get("items", []):
                    abstract = _clean_abstract(item.get("abstract", ""))
                    item_title = item.get("title", [""])[0] if isinstance(item.get("title"), list) else item.get("title", "")
                    if abstract and _title_similarity(title, item_title) >= 0.45:
                        return abstract
        except Exception:
            pass

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
        merged_keywords = _merge_unique_list(
            item.faculty_keywords,
            item.ai_keywords,
            item.themes,
            item.keywords,
        )
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
    """GET /api/search/?q=<query>"""
    result = run_search((request.query_params.get("q") or "").strip(), request)
    return Response(result)



def home(request):
    return HttpResponse(
        "<h1>Welcome to the Scoup Database!</h1><p>Go to <a href='/admin/'>Admin</a></p>"
    )

class FacultyListCreateView(generics.ListCreateAPIView):
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
    serializer_class = FacultySerializer

    def get_permissions(self):
        # Public browse (GET); require auth for create (POST)
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return Faculty.objects.filter(profile_visibility=True).filter(
            Q(is_approved=True) | Q(user__isnull=False)
        )

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


class FacultyDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = FacultyProfileSerializer
    permission_classes = [IsAuthenticated]
    queryset = Faculty.objects.all()

    def get_object(self):
        obj = super().get_object()
        requestor = _get_request_faculty(self.request.user)
        if self.request.user.is_staff or (requestor and obj.id == requestor.id):
            return obj
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
            is_approved=False,
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
    """
    POST /api/faculty/upload-cv-papers/
    Upload a CV/resume PDF. Uses OpenAI to extract structured data,
    then enriches papers with real abstracts from CrossRef/Semantic Scholar.
    Returns extracted items for faculty review — nothing is saved yet.
    """
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        faculty = _get_request_faculty(request.user, create_if_missing=True)
        if not faculty:
            return Response(
                {"detail": "Faculty profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        file = request.FILES.get("file")
        if not file:
            return Response({"error": "No PDF uploaded."}, status=400)

        # ── 1. Extract text from PDF ──────────────────────────────────────
        try:
            with pdfplumber.open(file) as pdf:
                full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])
        except Exception as e:
            return Response({"error": f"Could not read PDF: {str(e)}"}, status=400)

        if not full_text.strip():
            return Response({"error": "PDF appears to be empty or scanned (no readable text)."}, status=400)

        # ── 2. OpenAI extraction ──────────────────────────────────────────
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return Response({"error": "OpenAI API key not configured."}, status=500)

        client = OpenAI(api_key=api_key)  # OpenAI imported at top

        prompt = """You are extracting structured academic information from a faculty CV or resume.
Return a JSON object with exactly these keys:

{
  "profile": {
    "title": "academic title only e.g. Professor, Assistant Professor, Associate Professor, or empty string",
    "bio": "2-3 sentence professional bio based on their work and expertise, or empty string if insufficient info",
    "department": "department name or empty string"
  },
  "papers": [
    {
      "title": "full paper title",
      "year": 2023,
      "journal": "journal or conference name or empty string",
      "doi": "DOI string only — e.g. 10.1016/j.xxx or null. Strip any URL prefix like https://doi.org/",
      "authors": "author list as string or empty string"
    }
  ],
  "patents": [
    {
      "title": "patent title",
      "patent_number": "patent number or empty string",
      "year": 2020,
      "inventors": "inventor names or empty string"
    }
  ],
  "projects": [
    {
      "title": "project title",
      "description": "brief description or empty string",
      "year": 2022
    }
  ]
}

Rules:
- Extract ALL papers from any section labelled: Publications, Research, Published Intellectual Contributions, Articles, Journal Articles, Conference Papers, Book Chapters, Refereed Articles, or similar
- For each paper: capture the full title, year, journal/conference, and DOI if present
- DOIs appear as bare strings (10.xxxx/...) or as links (https://doi.org/10.xxxx/...) — extract just the DOI part after doi.org/
- Do NOT hallucinate DOIs — only include if explicitly present in the text
- Do NOT deduplicate — list every paper you find, even if similar titles exist
- Year must be an integer or null
- Return valid JSON only"""

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": full_text[:14000]},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            extracted = json.loads(resp.choices[0].message.content)  # json imported at top
        except Exception as e:
            return Response({"error": f"AI extraction failed: {str(e)}"}, status=500)

        raw_papers = extracted.get("papers", [])
        patents = extracted.get("patents", [])
        projects = extracted.get("projects", [])
        profile_info = extracted.get("profile", {})

        # ── Normalise DOI links → bare DOI ───────────────────────────────────
        # Some CVs list "https://doi.org/10.xxxx/..." — strip the URL prefix
        for p in raw_papers:
            doi = (p.get("doi") or "").strip()
            if doi:
                doi = _DOI_URL_PREFIX.sub("", doi).strip()
                p["doi"] = doi or None

        # ── Deduplicate extracted papers (DOI first, then title) ─────────────
        seen_dois: set = set()
        seen_titles: set = set()
        papers = []
        for p in raw_papers:
            doi = (p.get("doi") or "").strip().lower()
            title_key = re.sub(r"[^a-z0-9]", "", (p.get("title") or "").lower())
            if doi and doi in seen_dois:
                continue
            if title_key and title_key in seen_titles:
                continue
            if doi:
                seen_dois.add(doi)
            if title_key:
                seen_titles.add(title_key)
            papers.append(p)

        # ── 3. Enrich papers with real abstracts from CrossRef / Semantic Scholar ──
        for paper in papers:
            title = paper.get("title", "")
            doi = paper.get("doi")
            paper["abstract"] = fetch_abstract(title, doi) or ""

        return Response({
            "profile": profile_info,
            "papers": papers,
            "patents": patents,
            "projects": projects,
        })

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def bulk_publish_papers(request):
    """
    POST /api/faculty/papers/bulk-publish/
    Body: { "ids": [1,2,3] }  OR  { "all_draft": true }
    Sets status='published' on all specified (or all draft) papers belonging to this faculty.
    """
    faculty = _get_request_faculty(request.user, create_if_missing=False)
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=404)

    if request.data.get("all_draft"):
        qs = Paper.objects.filter(authors=faculty, status=Paper.STATUS_DRAFT)
    else:
        ids = [int(i) for i in (request.data.get("ids") or []) if str(i).isdigit()]
        qs = Paper.objects.filter(id__in=ids, authors=faculty)

    count = qs.update(status=Paper.STATUS_PUBLISHED)
    return Response({"published": count})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def extract_abstract_from_pdf(request):
    """
    POST /api/faculty/extract-abstract/
    Upload a paper PDF; returns AI-generated abstract extracted from its full text.
    """
    file = request.FILES.get("file")
    if not file:
        return Response({"error": "No file uploaded."}, status=400)

    try:
        with pdfplumber.open(file) as pdf:
            text = "\n".join([p.extract_text() or "" for p in pdf.pages])
    except Exception as e:
        return Response({"error": f"Could not read PDF: {str(e)}"}, status=400)

    if not text.strip():
        return Response({"error": "PDF appears to be scanned or has no readable text."}, status=400)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return Response({"error": "OpenAI not configured."}, status=500)

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert academic editor. Given the full text of a research paper, "
                        "extract or reconstruct the abstract. If an abstract is present in the text, return it verbatim. "
                        "If no abstract is found, write a concise 3-5 sentence academic abstract that accurately "
                        "summarises the paper's purpose, methods, and findings. "
                        "Return ONLY the abstract text — no labels, no preamble."
                    ),
                },
                {"role": "user", "content": text[:12000]},
            ],
            temperature=0.2,
        )
        abstract = resp.choices[0].message.content.strip()
    except Exception as e:
        return Response({"error": f"AI extraction failed: {str(e)}"}, status=500)

    return Response({"abstract": abstract})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_faculty_bio(request):
    """
    POST /api/faculty/generate-bio/
    Body: { "name", "title", "department", "qualifications": [...], "research_interests", "keywords": [...] }
    Returns a real AI-generated professional bio.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return Response({"error": "AI not available."}, status=503)

    data = request.data
    name = (data.get("name") or "").strip()
    title = (data.get("title") or "").strip()
    department = (data.get("department") or "").strip()
    qualifications = data.get("qualifications") or []
    research_interests = (data.get("research_interests") or "").strip()
    keywords = data.get("keywords") or []

    qual_text = "; ".join(
        f"{q.get('degree','')} from {q.get('institution','')} ({q.get('year','')})"
        for q in qualifications if q.get("degree")
    ) if qualifications else ""

    context = f"""Faculty profile details:
- Name: {name or 'Faculty member'}
- Title: {title or 'Faculty'}
- Department: {department or 'not specified'}
- Qualifications: {qual_text or 'not specified'}
- Research interests: {research_interests or 'not specified'}
- Expertise keywords: {', '.join(keywords) if keywords else 'not specified'}"""

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert academic writer. Write a concise, professional 2-3 sentence "
                        "third-person bio for a faculty member. The bio should highlight their academic title, "
                        "department, research focus, and any notable qualifications. "
                        "Sound natural and specific to their actual field — no generic filler. "
                        "Return only the bio text, no labels or preamble."
                    ),
                },
                {"role": "user", "content": context},
            ],
            temperature=0.4,
        )
        bio = resp.choices[0].message.content.strip()
    except Exception as e:
        return Response({"error": f"AI generation failed: {str(e)}"}, status=500)

    return Response({"bio": bio})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_research_interests(request):
    """
    POST /api/faculty/generate-research-interests/
    Body: { "name", "title", "department", "qualifications", "keywords": [...], "papers": [...] }
    Returns AI-generated research interest areas as a comma-separated string.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return Response({"error": "AI not available."}, status=503)

    data = request.data
    title = (data.get("title") or "").strip()
    department = (data.get("department") or "").strip()
    qualifications = data.get("qualifications") or []
    keywords = data.get("keywords") or []
    papers = data.get("papers") or []  # list of paper titles

    qual_text = "; ".join(
        f"{q.get('degree','')} in {q.get('institution','')}" for q in qualifications if q.get("degree")
    ) if qualifications else ""

    paper_titles = "; ".join(str(p) for p in papers[:10]) if papers else ""

    context = f"""Faculty profile:
- Title: {title or 'Faculty'}
- Department: {department or 'not specified'}
- Education: {qual_text or 'not specified'}
- Existing keywords: {', '.join(keywords) if keywords else 'none'}
- Recent paper titles: {paper_titles or 'none provided'}"""

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an academic profile specialist. Based on the faculty member's department, "
                        "education, and paper titles, generate 6-10 specific research interest areas. "
                        "Return them as a comma-separated list of concise phrases. "
                        "Be specific to their actual field — no generic terms. "
                        "Example: 'Machine Learning, Natural Language Processing, Healthcare Informatics, Predictive Analytics'"
                    ),
                },
                {"role": "user", "content": context},
            ],
            temperature=0.3,
        )
        interests = resp.choices[0].message.content.strip()
    except Exception as e:
        return Response({"error": f"AI generation failed: {str(e)}"}, status=500)

    return Response({"research_interests": interests})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_faculty_keywords(request):
    """
    POST /api/faculty/generate-profile-keywords/
    Generates keyword tags for the faculty profile based on their department, bio, research interests.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return Response({"error": "AI not available."}, status=503)

    data = request.data
    department = (data.get("department") or "").strip()
    bio = (data.get("bio") or "").strip()
    research_interests = (data.get("research_interests") or "").strip()
    title = (data.get("title") or "").strip()

    context = f"Department: {department}\nTitle: {title}\nBio: {bio}\nResearch interests: {research_interests}"

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate 8-12 academic keyword tags for a faculty profile. "
                        "Return ONLY a JSON array of short keyword strings. "
                        'Example: ["Machine Learning", "Data Science", "Healthcare AI"]'
                    ),
                },
                {"role": "user", "content": context},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = json.loads(resp.choices[0].message.content)
        keywords = raw.get("keywords") or raw.get("items") or next(
            (v for v in raw.values() if isinstance(v, list)), []
        )
        keywords = [str(k).strip() for k in keywords if k][:12]
    except Exception as e:
        return Response({"error": f"AI generation failed: {str(e)}"}, status=500)

    return Response({"keywords": keywords})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def generate_paper_keywords(request):
    """
    POST /api/faculty/generate-keywords/
    Body: { "title": "...", "abstract": "..." }
    Returns up to 8 AI-generated keywords.
    """
    title = (request.data.get("title") or "").strip()
    abstract = (request.data.get("abstract") or "").strip()
    if not title:
        return Response({"error": "Title is required."}, status=400)

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return Response({"error": "OpenAI not configured."}, status=500)

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an academic metadata specialist. Given a paper title and optional abstract, "
                        "generate 6-8 precise academic keywords. Return ONLY a JSON array of keyword strings. "
                        "Keywords should be specific, searchable, and relevant to the paper's topic, methods, and field. "
                        'Example: ["Machine Learning", "Healthcare", "Predictive Modeling", "Clinical Data"]'
                    ),
                },
                {
                    "role": "user",
                    "content": f"Title: {title}\n\nAbstract: {abstract}" if abstract else f"Title: {title}",
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw = json.loads(resp.choices[0].message.content)
        # Handle both {"keywords": [...]} and bare array wrapped in object
        keywords = raw.get("keywords") or raw.get("items") or next(
            (v for v in raw.values() if isinstance(v, list)), []
        )
        keywords = [str(k).strip() for k in keywords if k][:8]
    except Exception as e:
        return Response({"error": f"AI generation failed: {str(e)}"}, status=500)

    return Response({"keywords": keywords})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def paper_search_external(request):
    """
    GET /api/faculty/paper-search/?q=<title or DOI>
    Searches CrossRef and Semantic Scholar. Returns up to 10 candidates with abstract.
    """
    query = (request.GET.get("q") or "").strip()
    if not query:
        return Response({"results": []})

    headers = {"User-Agent": "SCOUP/1.0 (mailto:scoupteam@gmail.com)"}

    results = []

    # If query looks like a DOI — do an exact lookup
    bare = _DOI_URL_PREFIX.sub("", query).strip()
    is_doi = bool(re.match(r"10\.\d{4,}/", bare))

    if is_doi:
        try:
            r = http_requests.get(
                f"https://api.crossref.org/works/{bare}",
                timeout=8, headers=headers,
            )
            if r.ok:
                msg = r.json().get("message", {})
                title_list = msg.get("title", [])
                title = title_list[0] if title_list else ""
                results.append({
                    "title": title,
                    "doi": bare,
                    "journal": (msg.get("container-title") or [""])[0],
                    "year": (msg.get("published", {}).get("date-parts") or [[None]])[0][0],
                    "authors": ", ".join(
                        f"{a.get('given','')} {a.get('family','')}".strip()
                        for a in (msg.get("author") or [])
                    ),
                    "abstract": _clean_abstract(msg.get("abstract", "")),
                    "source": "CrossRef",
                })
        except Exception:
            pass
    else:
        # Semantic Scholar title search
        try:
            r = http_requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "fields": "title,abstract,year,authors,externalIds,venue", "limit": 8},
                timeout=8,
            )
            if r.ok:
                for item in r.json().get("data", []):
                    doi = (item.get("externalIds") or {}).get("DOI") or None
                    results.append({
                        "title": item.get("title", ""),
                        "doi": doi,
                        "journal": item.get("venue", ""),
                        "year": item.get("year"),
                        "authors": ", ".join(a.get("name", "") for a in (item.get("authors") or [])),
                        "abstract": item.get("abstract", ""),
                        "source": "Semantic Scholar",
                    })
        except Exception:
            pass

        # CrossRef title search (top 5)
        try:
            r = http_requests.get(
                "https://api.crossref.org/works",
                params={"query.title": query, "rows": 5, "select": "title,abstract,DOI,container-title,published,author"},
                timeout=8, headers=headers,
            )
            if r.ok:
                for item in r.json().get("message", {}).get("items", []):
                    doi = item.get("DOI", "")
                    # Skip if already in results
                    if any(d.get("doi", "").lower() == doi.lower() for d in results if doi):
                        continue
                    title_list = item.get("title", [])
                    results.append({
                        "title": title_list[0] if title_list else "",
                        "doi": doi or None,
                        "journal": (item.get("container-title") or [""])[0],
                        "year": (item.get("published", {}).get("date-parts") or [[None]])[0][0],
                        "authors": ", ".join(
                            f"{a.get('given','')} {a.get('family','')}".strip()
                            for a in (item.get("author") or [])
                        ),
                        "abstract": _clean_abstract(item.get("abstract", "")),
                        "source": "CrossRef",
                    })
        except Exception:
            pass

    return Response({"results": results[:10]})


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

class IsAdminUser(IsAuthenticated):
    """Allows access only to staff/superuser accounts."""
    def has_permission(self, request, view):
        return bool(super().has_permission(request, view) and (request.user.is_staff or request.user.is_superuser))


class AdminSchoolListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/admin/schools/  — list all schools
    POST /api/admin/schools/  — create a new school
    """
    permission_classes = [IsAdminUser]
    serializer_class = SchoolSerializer
    queryset = School.objects.all().order_by("display_order", "name")


class AdminSchoolDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/admin/schools/<id>/  — get school detail
    PATCH  /api/admin/schools/<id>/  — update school (name, code, display_order, is_active)
    DELETE /api/admin/schools/<id>/  — delete school (will fail if departments exist — protected)
    """
    permission_classes = [IsAdminUser]
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
    permission_classes = [IsAdminUser]
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
    permission_classes = [IsAdminUser]
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
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    qs = Faculty.objects.prefetch_related(
        "departments", "schools"
    ).select_related("primary_department", "primary_school").order_by("last_name", "first_name")

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))

    missing_only = request.query_params.get("missing", "").lower() == "true"
    if missing_only:
        qs = qs.filter(primary_department__isnull=True)

    # Pending: self-registered, verified institutional email, not yet approved
    pending_only = request.query_params.get("pending", "").lower() == "true"
    if pending_only:
        qs = qs.filter(user__isnull=False, institutional_email_verified=True, is_approved=False)

    results = []
    for f in qs:
        departments = [d.name for d in f.departments.all()]
        schools = [s.name for s in f.schools.all()]
        results.append({
            "id": f.id,
            "name": f.name or f"{f.first_name or ''} {f.last_name or ''}".strip() or f.faculty_id,
            "email": f.email or "",
            "institutional_email": f.institutional_email or "",
            "institutional_email_verified": f.institutional_email_verified,
            "is_approved": f.is_approved,
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
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
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
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
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
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
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
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        project = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    project.delete()
    return Response({"detail": "Project deleted."}, status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_patent_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        patent = Patent.objects.get(pk=pk)
    except Patent.DoesNotExist:
        return Response({"detail": "Patent not found."}, status=status.HTTP_404_NOT_FOUND)
    patent.delete()
    return Response({"detail": "Patent deleted."}, status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Password Reset (faculty accounts)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([AllowAny])
def forgot_password(request):
    """
    POST /api/auth/forgot-password/
    Body: { "email": "user@example.com" }

    Always returns 200 so we don't expose whether an email exists.
    Sends a reset link to the address if a matching faculty account exists.
    """
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        faculty = Faculty.objects.get(email__iexact=email)
        user = faculty.user
    except Faculty.DoesNotExist:
        # Silent — don't reveal whether the email exists
        return Response({"detail": "If that email is registered, a reset link has been sent."})
    except Exception:
        return Response({"detail": "If that email is registered, a reset link has been sent."})

    # Generate a secure token and store it in the cache for 1 hour
    token = secrets.token_urlsafe(32)
    cache_key = f"pwd_reset_{token}"
    cache.set(cache_key, user.pk, timeout=3600)

    # Build the reset URL — uses the frontend origin from the request
    origin = request.headers.get("Origin") or request.build_absolute_uri("/").rstrip("/")
    reset_url = f"{origin}/reset-password?token={token}"

    try:
        send_mail(
            subject="SCOUP — Password Reset Request",
            message=(
                f"Hello {faculty.first_name or user.username},\n\n"
                f"We received a request to reset your SCOUP password.\n\n"
                f"Click the link below to choose a new password (valid for 1 hour):\n"
                f"{reset_url}\n\n"
                f"If you did not request this, you can safely ignore this email.\n\n"
                f"— The SCOUP Team"
            ),
            from_email=None,  # uses DEFAULT_FROM_EMAIL from settings
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception:
        pass  # Never expose mail errors to the client

    return Response({"detail": "If that email is registered, a reset link has been sent."})


@api_view(["POST"])
@permission_classes([AllowAny])
def reset_password(request):
    """
    POST /api/auth/reset-password/
    Body: { "token": "...", "password": "new_password" }
    """
    token    = (request.data.get("token") or "").strip()
    password = (request.data.get("password") or "").strip()

    if not token or not password:
        return Response(
            {"detail": "Token and new password are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(password) < 8:
        return Response(
            {"detail": "Password must be at least 8 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    cache_key = f"pwd_reset_{token}"
    user_pk = cache.get(cache_key)

    if not user_pk:
        return Response(
            {"detail": "This reset link is invalid or has expired."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(pk=user_pk)
    except User.DoesNotExist:
        return Response(
            {"detail": "User not found."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user.set_password(password)
    user.save()
    cache.delete(cache_key)  # one-time use

    return Response({"detail": "Password updated successfully. You can now log in."})


# ---------------------------------------------------------------------------
# OTP email verification
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_institutional_otp(request):
    """
    POST /api/auth/send-otp/
    Body: { "institutional_email": "name@salisbury.edu" }
    Sends a 6-digit OTP to the given address. The faculty must be logged in.
    """
    institutional_email = (request.data.get("institutional_email") or "").strip().lower()
    if not institutional_email:
        return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        faculty = request.user.faculty_profile
    except Exception:
        return Response({"detail": "Faculty profile not found."}, status=status.HTTP_404_NOT_FOUND)

    # Generate 6-digit OTP using a cryptographically secure source
    otp = f"{secrets.randbelow(1_000_000):06d}"
    cache_key = f"email_otp_{faculty.id}"
    cache.set(cache_key, {"otp": otp, "email": institutional_email}, timeout=600)

    try:
        send_mail(
            subject="SCOUP — Your Verification Code",
            message=(
                f"Hello,\n\n"
                f"Your SCOUP email verification code is:\n\n"
                f"    {otp}\n\n"
                f"This code expires in 10 minutes.\n\n"
                f"If you did not request this, please ignore this email.\n\n"
                f"— The SCOUP Team"
            ),
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[institutional_email],
            fail_silently=False,
        )
    except Exception as e:
        return Response({"detail": f"Failed to send email: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response({"detail": "Verification code sent."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_institutional_otp(request):
    """
    POST /api/auth/verify-otp/
    Body: { "otp": "123456" }
    Verifies the OTP. On success marks institutional_email_verified=True
    and notifies admin.
    """
    otp_input = (request.data.get("otp") or "").strip()
    if not otp_input:
        return Response({"detail": "Code is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        faculty = request.user.faculty_profile
    except Exception:
        return Response({"detail": "Faculty profile not found."}, status=status.HTTP_404_NOT_FOUND)

    cache_key = f"email_otp_{faculty.id}"
    cached = cache.get(cache_key)

    if not cached:
        return Response({"detail": "Code has expired. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)

    if cached["otp"] != otp_input:
        return Response({"detail": "Incorrect code. Please try again."}, status=status.HTTP_400_BAD_REQUEST)

    # Mark verified and auto-approve — verifying the institutional email is proof of faculty status
    faculty.institutional_email = cached["email"]
    faculty.institutional_email_verified = True
    faculty.is_approved = True
    faculty.save(update_fields=["institutional_email", "institutional_email_verified", "is_approved", "updated_at"])
    cache.delete(cache_key)

    # Notify admin
    admin_emails = list(
        User.objects.filter(is_staff=True).values_list("email", flat=True)
    )
    admin_emails = [e for e in admin_emails if e]
    if admin_emails:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or request.user.username
        try:
            send_mail(
                subject="SCOUP — New Faculty Awaiting Approval",
                message=(
                    f"A faculty member has verified their institutional email and is awaiting profile approval.\n\n"
                    f"Name: {faculty_name}\n"
                    f"Institutional Email: {cached['email']}\n"
                    f"Login Email: {faculty.email or request.user.email or 'N/A'}\n\n"
                    f"Log in to the admin dashboard to review and approve their profile.\n\n"
                    f"— SCOUP System"
                ),
                from_email=None,
                recipient_list=admin_emails,
                fail_silently=True,
            )
        except Exception:
            pass  # Don't fail the request if admin notification fails

    return Response({"detail": "Email verified successfully. Your profile is now pending admin approval."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_approve_faculty(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    faculty.is_approved = True
    faculty.save(update_fields=["is_approved", "updated_at"])

    # Email the faculty
    recipient = faculty.institutional_email or faculty.email
    if recipient:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
        try:
            send_mail(
                subject="SCOUP — Your Profile Has Been Approved!",
                message=(
                    f"Hi {faculty_name},\n\n"
                    f"Great news! Your SCOUP profile has been reviewed and approved.\n\n"
                    f"You are now visible in SCOUP's faculty search and collaboration network.\n\n"
                    f"Log in to your dashboard to complete your profile and start connecting.\n\n"
                    f"— The SCOUP Team"
                ),
                from_email=None,
                recipient_list=[recipient],
                fail_silently=True,
            )
        except Exception:
            pass

    return Response({"detail": "Faculty approved and notified."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_reject_faculty(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    reason = (request.data.get("reason") or "").strip()

    faculty.is_approved = False
    faculty.institutional_email_verified = False
    faculty.save(update_fields=["is_approved", "institutional_email_verified", "updated_at"])

    recipient = faculty.institutional_email or faculty.email
    if recipient:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
        reason_line = f"\nReason: {reason}\n" if reason else ""
        try:
            send_mail(
                subject="SCOUP — Profile Verification Update",
                message=(
                    f"Hi {faculty_name},\n\n"
                    f"We were unable to verify your SCOUP faculty profile at this time.{reason_line}\n"
                    f"If you believe this is an error or have questions, please contact us at scoupteam@gmail.com.\n\n"
                    f"— The SCOUP Team"
                ),
                from_email=None,
                recipient_list=[recipient],
                fail_silently=True,
            )
        except Exception:
            pass

    return Response({"detail": "Faculty rejected and notified."})


# ---------------------------------------------------------------------------
# CV import — confirm & save approved items
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm_cv_items(request):
    """
    POST /api/faculty/confirm-cv-items/
    Body:
    {
      "profile": { "title": "...", "bio": "...", "department": "..." },
      "papers":  [ { "title": "...", "year": 2023, "journal": "...", "doi": "...", "abstract": "...", "authors": "..." } ],
      "patents": [ { "title": "...", "patent_number": "...", "year": 2020, "inventors": "..." } ],
      "projects": [ { "title": "...", "description": "...", "year": 2022 } ]
    }
    Saves only the items the faculty approved. Skips duplicates.
    """
    faculty = _get_request_faculty(request.user, create_if_missing=False)
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=status.HTTP_404_NOT_FOUND)

    saved = {"papers": 0, "patents": 0, "projects": 0, "profile_updated": False}

    # ── Profile fields ────────────────────────────────────────────────────
    profile_data = request.data.get("profile") or {}
    profile_changed = False
    if profile_data.get("title") and not faculty.title:
        faculty.title = profile_data["title"]
        profile_changed = True
    if profile_data.get("bio") and not faculty.bio:
        faculty.bio = profile_data["bio"]
        profile_changed = True
    if profile_changed:
        faculty.save(update_fields=["title", "bio", "updated_at"])
        saved["profile_updated"] = True

    # ── Papers ────────────────────────────────────────────────────────────
    for p in (request.data.get("papers") or []):
        title = (p.get("title") or "").strip()
        doi = (p.get("doi") or "").strip() or None
        if not title:
            continue

        # Convert bare year integer → "YYYY-01-01" date string
        raw_year = p.get("year")
        date_published = f"{raw_year}-01-01" if raw_year else None

        if doi:
            paper, _ = Paper.objects.get_or_create(
                doi=doi,
                defaults={
                    "title": title,
                    "journal": p.get("journal") or "",
                    "abstract": p.get("abstract") or "",
                    "date_published": date_published,
                },
            )
        else:
            # Match by title to avoid duplicates
            paper = Paper.objects.filter(title__iexact=title).first()
            if not paper:
                paper = Paper.objects.create(
                    doi=f"cv-import-{uuid.uuid4().hex[:12]}",
                    title=title,
                    journal=p.get("journal") or "",
                    abstract=p.get("abstract") or "",
                    date_published=date_published,
                )
        paper.status = Paper.STATUS_DRAFT
        paper.save(update_fields=["status"])
        paper.authors.add(faculty)
        saved["papers"] += 1

    # ── Patents ───────────────────────────────────────────────────────────
    for p in (request.data.get("patents") or []):
        title = (p.get("title") or "").strip()
        if not title:
            continue
        patent_number = (p.get("patent_number") or "").strip() or f"cv-patent-{uuid.uuid4().hex[:10]}"
        raw_year = p.get("year")
        filing_date = f"{raw_year}-01-01" if raw_year else None
        patent, _ = Patent.objects.get_or_create(
            patent_number=patent_number,
            defaults={
                "title": title,
                "abstract": p.get("abstract") or "",
                "filing_date": filing_date,
            },
        )
        patent.faculty.add(faculty)
        saved["patents"] += 1

    # ── Projects ──────────────────────────────────────────────────────────
    for p in (request.data.get("projects") or []):
        title = (p.get("title") or "").strip()
        if not title:
            continue
        raw_year = p.get("year")
        start_date = f"{raw_year}-01-01" if raw_year else None
        # Check if this faculty already has a project with this title to avoid
        # accidentally linking to an unrelated project with the same name.
        project = Project.objects.filter(title__iexact=title, faculty=faculty).first()
        if not project:
            project = Project.objects.create(
                title=title,
                description=p.get("description") or "",
                start_date=start_date,
            )
        project.faculty.add(faculty)
        saved["projects"] += 1

    return Response({
        "detail": "Items saved successfully.",
        "saved": saved,
    })
