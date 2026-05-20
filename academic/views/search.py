from collections import defaultdict, Counter

from django.db.models import Prefetch, Q
from django.db.utils import OperationalError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ..models import Faculty, Paper, Patent, Project
from ..search_engine import run_search, QUERY_EXPANSIONS
from ..semantic import cosine_similarity, create_query_embedding
from .utils import (
    _normalize_keyword_list,
    _year_from_dates,
    _normalize_paper_link,
    _full_name,
    _merge_unique_list,
    _faculty_school_names,
    _faculty_department_names,
    _is_confirmed_su_faculty,
    _get_request_faculty,
)


COLLABORATION_BRIDGES = [
    {
        "label": "Digital humanities",
        "left": ["natural language processing", "nlp", "text mining", "computational linguistics", "machine learning", "artificial intelligence", "ai", "data science"],
        "right": ["shakespeare", "shakespearean", "literature", "literary", "drama", "poetry", "theatre", "english", "arts", "humanities", "rhetoric", "archive", "text"],
        "terms": ["digital humanities", "computational text analysis", "corpus linguistics", "literary analysis", "text mining", "computational linguistics"],
    },
    {
        "label": "Computational biology",
        "left": ["machine learning", "artificial intelligence", "ai", "data science", "computer science", "software", "algorithm", "analytics"],
        "right": ["biology", "biological", "genomics", "ecology", "marine", "health", "medical", "clinical", "neuroscience"],
        "terms": ["bioinformatics", "computational biology", "biomedical informatics", "data-driven biology", "predictive modeling"],
    },
    {
        "label": "Learning analytics",
        "left": ["machine learning", "artificial intelligence", "ai", "data science", "analytics", "software", "technology"],
        "right": ["education", "learning", "teaching", "pedagogy", "student", "curriculum", "classroom"],
        "terms": ["learning analytics", "educational technology", "student success analytics", "human-computer interaction"],
    },
    {
        "label": "Environmental data science",
        "left": ["machine learning", "artificial intelligence", "ai", "data science", "gis", "geospatial", "modeling", "analytics"],
        "right": ["environment", "environmental", "climate", "sustainability", "ecology", "water", "coastal", "marine"],
        "terms": ["environmental data science", "geospatial analytics", "climate modeling", "sustainability analytics"],
    },
    {
        "label": "Business analytics",
        "left": ["machine learning", "artificial intelligence", "ai", "data science", "analytics", "optimization", "software"],
        "right": ["business", "marketing", "management", "finance", "economics", "operations", "entrepreneurship"],
        "terms": ["business analytics", "decision science", "marketing analytics", "operations research", "predictive analytics"],
    },
]


def _query_contains_any(query_text, terms):
    normalized = f" {query_text} "
    return any(f" {term} " in normalized for term in terms)


def _collaboration_intent_query(query):
    normalized = f" {_normalize_query_text(query)} "
    expanded_terms = []
    categories = []

    for bridge in COLLABORATION_BRIDGES:
        left_match = _query_contains_any(normalized, bridge["left"])
        right_match = _query_contains_any(normalized, bridge["right"])
        if left_match and right_match:
            categories.append(bridge["label"])
            expanded_terms.extend(bridge["terms"])

    for raw, expansion in QUERY_EXPANSIONS.items():
        if _query_contains_any(normalized, [raw]):
            expanded_terms.append(expansion)

    expanded_terms = _merge_unique_list(expanded_terms)
    expanded_query = " ".join([query, *expanded_terms]).strip()
    return {
        "originalQuery": query,
        "expandedQuery": expanded_query,
        "suggestedCategories": categories,
        "expandedTerms": expanded_terms,
    }


def _normalize_query_text(value):
    text = str(value or "").lower()
    text = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(text.split())


def _faculty_network_keywords(faculty):
    keywords = _merge_unique_list(
        getattr(faculty, "keywords", None),
        getattr(faculty, "faculty_keywords", None),
        getattr(faculty, "ai_keywords", None),
        getattr(faculty, "themes", None),
    )
    papers = getattr(faculty, "_prefetched_objects_cache", {}).get("papers")
    if papers is None:
        papers = faculty.papers.all()[:20]
    for paper in papers:
        keywords = _merge_unique_list(
            keywords,
            getattr(paper, "keywords", None),
            getattr(paper, "ai_keywords", None),
            getattr(paper, "faculty_keywords", None),
            getattr(paper, "themes", None),
        )
    return keywords


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

    score = 30
    if not my_keyword_set:
        score = 36
    score += min(42, len(shared_keywords) * 14)
    if shared_keywords and not department_match:
        score += 8
    if department_match:
        score += 5 if shared_keywords else 2
    if school_match:
        score += 4 if shared_keywords else 2
    score += min(8, richness * 2)
    return min(96, score), shared_keywords[:6], department_match, school_match


def _network_reason(shared_keywords, department_match, school_match, fallback):
    if shared_keywords and department_match:
        return f"Shared expertise in {', '.join(shared_keywords[:3])} with department overlap."
    if shared_keywords and school_match:
        return f"Shared expertise in {', '.join(shared_keywords[:3])} within a related school."
    if shared_keywords:
        return f"Cross-disciplinary overlap in {', '.join(shared_keywords[:3])}."
    if department_match:
        return "Weak fit based only on department alignment."
    if school_match:
        return "Weak fit based only on school alignment."
    return fallback


def _matches_query(values, query):
    if not query:
        return True
    needle = query.lower()
    return any(needle in str(value or "").lower() for value in values)


def _blend_search_and_collaboration_score(search_confidence, collaboration_score):
    try:
        search_confidence = int(search_confidence or 0)
    except (TypeError, ValueError):
        search_confidence = 0
    try:
        collaboration_score = int(collaboration_score or 0)
    except (TypeError, ValueError):
        collaboration_score = 0
    return min(99, round((search_confidence * 0.75) + (collaboration_score * 0.25)))


def _combined_internal_reason(search_reason, collaboration_reason):
    search_reason = str(search_reason or "").strip()
    collaboration_reason = str(collaboration_reason or "").strip()
    if collaboration_reason.startswith("Search relevance is strong"):
        return search_reason or "Matches your collaboration idea based on available SCOUP data."
    if search_reason and collaboration_reason:
        return f"{search_reason} Collaboration fit: {collaboration_reason}"
    return search_reason or collaboration_reason or "Potential collaboration fit based on available SU data."


def _network_fit(candidate_keywords, candidate_departments, candidate_schools, my_keywords, my_departments, my_schools, richness=0):
    score, shared, department_match, school_match = _score_network_item(
        candidate_keywords,
        candidate_departments,
        candidate_schools,
        my_keywords,
        my_departments,
        my_schools,
        richness=richness,
    )
    reason = _network_reason(
        shared,
        department_match,
        school_match,
        "Search relevance is strong; no direct profile overlap was found yet.",
    )
    return score, shared, reason


def _network_payload_from_unified_search(search_payload, faculty, my_keywords, my_departments, my_schools, request, limit, collaboration_intent=None):
    base_results = search_payload.get("results", [])
    ids_by_type = {
        "faculty": [],
        "paper": [],
        "patent": [],
        "project": [],
    }
    for result in base_results:
        result_type = result.get("type")
        result_id = str((result.get("data") or {}).get("id") or "")
        if result_type in ids_by_type and result_id.isdigit():
            ids_by_type[result_type].append(int(result_id))

    faculty_map = {
        item.id: item
        for item in Faculty.objects.filter(id__in=ids_by_type["faculty"])
        .prefetch_related("schools", "departments", "papers")
        .select_related("primary_school", "primary_department", "user")
    }
    paper_map = {
        item.id: item
        for item in Paper.objects.filter(id__in=ids_by_type["paper"])
        .defer("paper_embedding", "embedding_model", "embedding_updated_at")
        .prefetch_related("authors", "authors__schools", "authors__departments")
    }
    patent_map = {
        item.id: item
        for item in Patent.objects.filter(id__in=ids_by_type["patent"])
        .prefetch_related("faculty", "faculty__schools", "faculty__departments")
    }
    project_map = {
        item.id: item
        for item in Project.objects.filter(id__in=ids_by_type["project"])
        .prefetch_related("faculty", "faculty__schools", "faculty__departments")
    }

    colleagues = []
    papers = []
    patents = []
    projects = []

    for result in base_results:
        data = result.get("data") or {}
        result_type = result.get("type")
        try:
            result_id = int(data.get("id"))
        except (TypeError, ValueError):
            continue
        search_confidence = result.get("confidence", 0)
        search_reason = result.get("aiJustification", "")

        if result_type == "faculty":
            item = faculty_map.get(result_id)
            if not item or item.id == faculty.id or not _is_confirmed_su_faculty(item):
                continue
            departments = _faculty_department_names(item)
            schools = _faculty_school_names(item)
            keywords = _faculty_network_keywords(item)
            fit_score, shared, fit_reason = _network_fit(
                keywords,
                departments,
                schools,
                my_keywords,
                my_departments,
                my_schools,
                richness=sum(bool(value) for value in [item.email, item.title, item.bio, departments, schools, keywords, item.article_count]),
            )
            name = _full_name(item.first_name, item.last_name, (item.name or "").strip() or item.faculty_id)
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
                    "matchScore": _blend_search_and_collaboration_score(search_confidence, fit_score),
                    "matchReason": _combined_internal_reason(search_reason, fit_reason),
                    "searchConfidence": search_confidence,
                    "collaborationScore": fit_score,
                    "articleCount": item.article_count or item.papers.count(),
                    "totalCitations": item.total_citations or 0,
                }
            )

        elif result_type == "paper":
            item = paper_map.get(result_id)
            if not item:
                continue
            authors = list(item.authors.all())
            if authors and not any(_is_confirmed_su_faculty(author) for author in authors):
                continue
            departments = _merge_unique_list(*[_faculty_department_names(author) for author in authors])
            schools = _merge_unique_list(*[_faculty_school_names(author) for author in authors])
            keywords = _merge_unique_list(item.keywords, item.ai_keywords, item.faculty_keywords, item.themes)
            author_names = [
                author.name or _full_name(author.first_name, author.last_name, author.faculty_id)
                for author in authors
            ]
            fit_score, shared, fit_reason = _network_fit(
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
                    "authors": author_names or data.get("authors", []),
                    "journal": item.journal or "",
                    "year": _year_from_dates(item.date_published_online, item.date_published_print, item.date_published) or 0,
                    "abstract": item.abstract or "",
                    "link": _normalize_paper_link(item.download_url, item.url, item.license_url, item.doi),
                    "citations": item.tc_count or 0,
                    "keywords": keywords,
                    "departments": departments,
                    "schools": schools,
                    "sharedKeywords": shared,
                    "relevanceScore": _blend_search_and_collaboration_score(search_confidence, fit_score),
                    "relevanceReason": _combined_internal_reason(search_reason, fit_reason),
                    "searchConfidence": search_confidence,
                    "collaborationScore": fit_score,
                    "isOpenToCollaboration": getattr(item, "is_open_to_collaboration", False),
                    "collaborationInvitation": getattr(item, "collaboration_invitation", "") or "",
                    "allowStudentInterest": getattr(item, "allow_student_interest", False),
                }
            )

        elif result_type == "patent":
            item = patent_map.get(result_id)
            if not item:
                continue
            members = list(item.faculty.all())
            if members and not any(_is_confirmed_su_faculty(member) for member in members):
                continue
            departments = _merge_unique_list(*[_faculty_department_names(member) for member in members])
            schools = _merge_unique_list(*[_faculty_school_names(member) for member in members])
            keywords = _normalize_keyword_list(item.aiKeywords)
            inventor_names = [
                member.name or _full_name(member.first_name, member.last_name, member.faculty_id)
                for member in members
            ]
            fit_score, shared, fit_reason = _network_fit(
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
                    "relevanceScore": _blend_search_and_collaboration_score(search_confidence, fit_score),
                    "relevanceReason": _combined_internal_reason(search_reason, fit_reason),
                    "searchConfidence": search_confidence,
                    "collaborationScore": fit_score,
                }
            )

        elif result_type == "project":
            item = project_map.get(result_id)
            if not item:
                continue
            members = list(item.faculty.all())
            if members and not any(_is_confirmed_su_faculty(member) for member in members):
                continue
            departments = _merge_unique_list(*[_faculty_department_names(member) for member in members])
            schools = _merge_unique_list(*[_faculty_school_names(member) for member in members])
            keywords = _normalize_keyword_list(item.keywords)
            lead_names = [
                member.name or _full_name(member.first_name, member.last_name, member.faculty_id)
                for member in members
            ]
            fit_score, shared, fit_reason = _network_fit(
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
                    "relevanceScore": _blend_search_and_collaboration_score(search_confidence, fit_score),
                    "relevanceReason": _combined_internal_reason(search_reason, fit_reason),
                    "searchConfidence": search_confidence,
                    "collaborationScore": fit_score,
                }
            )

    return {
        "query": (collaboration_intent or {}).get("originalQuery") or search_payload.get("query", ""),
        "expandedQuery": (collaboration_intent or {}).get("expandedQuery") or search_payload.get("query", ""),
        "suggestedCategories": (collaboration_intent or {}).get("suggestedCategories", []),
        "expandedTerms": (collaboration_intent or {}).get("expandedTerms", []),
        "limit": limit,
        "profileKeywords": my_keywords,
        "searchMode": "unified_with_collaboration",
        "colleagues": colleagues[:limit],
        "papers": papers[:limit],
        "patents": patents[:limit],
        "projects": projects[:limit],
    }


@api_view(["GET"])
@permission_classes([AllowAny])
def public_search_data(request):
    faculty_qs = (
        Faculty.objects.filter(profile_visibility=True)
        .filter(Q(is_approved=True) | Q(user__isnull=False))
        .exclude(user__is_staff=True)
        .exclude(user__is_superuser=True)
        .prefetch_related("projects", "patents", "departments", "schools")
        .select_related("user", "primary_department", "primary_school")
        .order_by("last_name", "first_name")
    )
    _faculty_with_affiliations = Faculty.objects.prefetch_related(
        "schools", "departments"
    ).select_related("primary_school", "primary_department")

    # Only published papers are visible publicly — drafts and in-review stay hidden
    papers_qs = (
        Paper.objects.filter(status=Paper.STATUS_PUBLISHED)
        .defer("paper_embedding", "embedding_model", "embedding_updated_at")
        .prefetch_related(Prefetch("authors", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )
    # Only projects linked to at least one visible approved faculty member
    approved_faculty_ids = set(faculty_qs.values_list("id", flat=True))
    projects_qs = (
        Project.objects.filter(faculty__id__in=approved_faculty_ids)
        .distinct()
        .prefetch_related(Prefetch("faculty", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )
    patents_qs = (
        Patent.objects.filter(faculty__id__in=approved_faculty_ids)
        .distinct()
        .prefetch_related(Prefetch("faculty", queryset=_faculty_with_affiliations))
        .order_by("-id")
    )

    faculty = []
    for item in faculty_qs:
        from .utils import _has_salisbury_department
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
                "email": item.email if getattr(item, "show_email_publicly", False) else "",
                "phone": item.phone if getattr(item, "show_phone_publicly", False) else "",
                "photo": photo_url,
                "bio": item.bio or "",
                "qualifications": item.qualifications or [],
                "allowMessagesViaSCOUP": getattr(item, "allow_messages_through_scoup", True),
                "researchInterests": merged_keywords[:8],
                "aiKeywords": merged_keywords,
                "metricsProfile": {
                    "totalCitations": item.total_citations or 0,
                    "articleCount": item.article_count or 0,
                    "averageCitations": item.average_citations or 0.0,
                },
                "themes": _normalize_keyword_list(item.themes),
                "journals": _normalize_keyword_list(item.journals),
                "nsfCategories": _normalize_keyword_list(item.keywords)[:6],
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
                "isOpenToCollaboration": item.is_open_to_collaboration,
                "collaborationInvitation": item.collaboration_invitation or "",
                "allowStudentInterest": item.allow_student_interest,
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

    my_keywords = _faculty_network_keywords(faculty)
    my_departments = _faculty_department_names(faculty)
    my_schools = _faculty_school_names(faculty)

    if query:
        collaboration_intent = _collaboration_intent_query(query)
        search_payload = run_search(collaboration_intent["expandedQuery"], request)
        return Response(
            _network_payload_from_unified_search(
                search_payload,
                faculty,
                my_keywords,
                my_departments,
                my_schools,
                request,
                limit,
                collaboration_intent,
            )
        )

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
        keywords = _faculty_network_keywords(item)
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
        if my_keywords and not shared and score < 50:
            continue
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


@api_view(["GET"])
@permission_classes([AllowAny])
def query_expansions(request):
    """GET /api/query-expansions/ — returns the abbreviation expansion map."""
    return Response(QUERY_EXPANSIONS)


@api_view(["GET"])
@permission_classes([AllowAny])
def categories_list(request):
    """
    GET /api/categories/
    Returns all top-level NSF taxonomy categories with article + faculty counts
    and their nested mid-level categories.
    """
    papers = list(
        Paper.objects.filter(status=Paper.STATUS_PUBLISHED)
        .only("id", "top_level_categories", "mid_level_categories")
        .prefetch_related("authors")
    )

    # top_cat -> { mid_cats: set, paper_ids: set, faculty_ids: set }
    top_map = defaultdict(lambda: {"mid_cats": set(), "paper_ids": set(), "faculty_ids": set()})
    # top_cat -> mid_cat -> { paper_ids, faculty_ids }
    mid_detail_map = defaultdict(lambda: defaultdict(lambda: {"paper_ids": set(), "faculty_ids": set()}))

    for paper in papers:
        tops = paper.top_level_categories or []
        mids = paper.mid_level_categories or []
        author_ids = [a.id for a in paper.authors.all()]
        for top in tops:
            top = top.strip()
            if not top:
                continue
            top_map[top]["paper_ids"].add(paper.id)
            top_map[top]["faculty_ids"].update(author_ids)
            for mid in mids:
                mid = mid.strip()
                if mid:
                    top_map[top]["mid_cats"].add(mid)
                    mid_detail_map[top][mid]["paper_ids"].add(paper.id)
                    mid_detail_map[top][mid]["faculty_ids"].update(author_ids)

    result = []
    for top_name, data in sorted(top_map.items()):
        mid_list = []
        for mid_name in sorted(data["mid_cats"]):
            mid_data = mid_detail_map[top_name][mid_name]
            mid_list.append({
                "name": mid_name,
                "slug": mid_name.lower().replace(" ", "-").replace(",", "").replace("(", "").replace(")", ""),
                "article_count": len(mid_data["paper_ids"]),
                "faculty_count": len(mid_data["faculty_ids"]),
            })
        result.append({
            "name": top_name,
            "slug": top_name.lower().replace(" ", "-").replace(",", "").replace("(", "").replace(")", ""),
            "article_count": len(data["paper_ids"]),
            "faculty_count": len(data["faculty_ids"]),
            "mid_level_categories": mid_list,
        })

    return Response(result)


@api_view(["GET"])
@permission_classes([AllowAny])
def category_detail(request, slug):
    """
    GET /api/categories/<slug>/
    Returns papers and faculty for a given top-level or mid-level category slug.
    Query params:
      ?level=top|mid  (default: tries both)
      ?page=1
      ?page_size=20
    """
    page = int(request.query_params.get("page", 1))
    page_size = min(int(request.query_params.get("page_size", 20)), 100)
    level = request.query_params.get("level", "")

    def to_slug(s):
        return s.lower().replace(" ", "-").replace(",", "").replace("(", "").replace(")", "")

    # Fetch all published papers and filter in Python (works on SQLite + PostgreSQL)
    all_papers = list(
        Paper.objects.filter(status=Paper.STATUS_PUBLISHED)
        .prefetch_related("authors")
        .order_by("-tc_count")
    )

    matched_papers = []
    category_name = slug  # fallback display name
    matched = False

    if level != "mid":
        # Try top-level match first
        for paper in all_papers:
            for top in (paper.top_level_categories or []):
                if to_slug(top.strip()) == slug:
                    if not matched:
                        category_name = top.strip()
                        matched = True
                    matched_papers.append(paper)
                    break

    if not matched and level != "top":
        # Try mid-level match
        for paper in all_papers:
            for mid in (paper.mid_level_categories or []):
                if to_slug(mid.strip()) == slug:
                    if not matched:
                        category_name = mid.strip()
                        matched = True
                    matched_papers.append(paper)
                    break

    if not matched:
        return Response({"error": "Category not found"}, status=404)

    # --- Aggregate themes across all matched papers ---
    theme_counter = Counter()
    # paper_id -> set of themes (for frontend filtering)
    paper_themes_map = {}
    for paper in matched_papers:
        themes = paper.themes or []
        paper_themes_map[paper.id] = themes
        for t in themes:
            t = t.strip()
            if t:
                theme_counter[t] += 1
    top_themes = [{"name": t, "count": c} for t, c in theme_counter.most_common(30)]

    # --- Collect faculty and build faculty -> paper_ids mapping ---
    faculty_ids = set()
    faculty_paper_map = {}   # faculty_id -> list of paper_ids
    for paper in matched_papers:
        for author in paper.authors.all():
            faculty_ids.add(author.id)
            faculty_paper_map.setdefault(author.id, []).append(paper.id)

    # Only show faculty who have profile_visibility=True in the public faculty tab
    faculty_qs = Faculty.objects.filter(
        id__in=faculty_ids, profile_visibility=True
    ).exclude(user__is_staff=True).exclude(user__is_superuser=True).order_by("-total_citations")

    # Department count
    departments = set()
    for f in faculty_qs:
        if f.department:
            departments.add(f.department.strip())

    # Citation stats across matched papers
    total_citations = sum(p.tc_count or 0 for p in matched_papers)
    citation_avg = round(total_citations / len(matched_papers), 1) if matched_papers else 0

    # --- Build full paper list (no pagination — client filters) ---
    all_paper_data = []
    for p in matched_papers:
        all_paper_data.append({
            "id": p.id,
            "title": p.title,
            "doi": p.doi,
            "journal": p.journal,
            "date_published": str(p.date_published) if p.date_published else None,
            "tc_count": p.tc_count or 0,
            "themes": paper_themes_map.get(p.id, []),
            "mid_level_categories": p.mid_level_categories or [],
            "download_url": p.download_url or None,
            "authors": [
                {
                    "id": a.id,
                    "name": (a.name or f"{a.first_name or ''} {a.last_name or ''}".strip()),
                }
                for a in p.authors.all()
            ],
        })

    # --- Build faculty list ---
    faculty_data = []
    for f in faculty_qs:
        paper_ids_for_faculty = faculty_paper_map.get(f.id, [])
        faculty_data.append({
            "id": f.id,
            "name": f.name or f"{f.first_name or ''} {f.last_name or ''}".strip(),
            "department": f.department,
            "title": f.title,
            "total_citations": f.total_citations,
            "article_count": f.article_count,
            "photo": request.build_absolute_uri(f.photo.url) if f.photo else None,
            "themes": (f.themes or [])[:8],
            "paper_ids": paper_ids_for_faculty,  # used for frontend click filtering
            "is_approved": f.is_approved,
            "profile_visibility": f.profile_visibility,
            "email": f.email or "",
        })

    return Response({
        "category_name": category_name,
        "slug": slug,
        # Summary stats
        "stats": {
            "article_count": len(matched_papers),
            "faculty_count": len(faculty_ids),
            "department_count": len(departments),
            "total_citations": total_citations,
            "citation_average": citation_avg,
        },
        # Panels
        "themes": top_themes,
        "faculty": faculty_data,
        "papers": all_paper_data,
    })
