
import re
import uuid

import pdfplumber
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
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
)
from .serializers import (
    FacultyProfileSerializer,
    FacultySerializer,
    PaperSerializer,
    PatentSerializer,
    ProjectSerializer,
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
    internal.dois = _merge_unique_list(internal.dois, external.dois)
    internal.titles = _merge_unique_list(internal.titles, external.titles)
    internal.categories = _merge_unique_list(internal.categories, external.categories)
    internal.top_level_categories = _merge_unique_list(
        internal.top_level_categories, external.top_level_categories
    )
    internal.mid_level_categories = _merge_unique_list(
        internal.mid_level_categories, external.mid_level_categories
    )
    internal.low_level_categories = _merge_unique_list(
        internal.low_level_categories, external.low_level_categories
    )
    internal.category_urls = _merge_unique_list(internal.category_urls, external.category_urls)
    internal.top_category_urls = _merge_unique_list(
        internal.top_category_urls, external.top_category_urls
    )
    internal.mid_category_urls = _merge_unique_list(
        internal.mid_category_urls, external.mid_category_urls
    )
    internal.low_category_urls = _merge_unique_list(
        internal.low_category_urls, external.low_category_urls
    )
    internal.themes = _merge_unique_list(internal.themes, external.themes)
    internal.journals = _merge_unique_list(internal.journals, external.journals)
    internal.keywords = _merge_unique_list(internal.keywords, external.keywords)
    internal.faculty_keywords = ", ".join(
        _merge_unique_list(internal.faculty_keywords, external.faculty_keywords)
    )
    internal.ai_keywords = ", ".join(
        _merge_unique_list(internal.ai_keywords, external.ai_keywords)
    )

    merged_source_profile = {}
    if isinstance(external.source_profile, dict):
        merged_source_profile.update(external.source_profile)
    if isinstance(internal.source_profile, dict):
        merged_source_profile.update(internal.source_profile)
    internal.source_profile = merged_source_profile
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
        .prefetch_related("projects", "patents")
        .select_related("user")
        .order_by("last_name", "first_name")
    )
    papers_qs = (
        Paper.objects.defer("paper_embedding", "embedding_model", "embedding_updated_at")
        .prefetch_related("authors")
        .order_by("-id")
    )
    projects_qs = Project.objects.all().prefetch_related("faculty").order_by("-id")
    patents_qs = Patent.objects.all().prefetch_related("faculty").order_by("-id")

    faculty = []
    for item in faculty_qs:
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

        faculty.append(
            {
                "id": str(item.id),
                "name": full_name,
                "title": item.title or "",
                "department": item.department or "",
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
                "categories": {
                    "top": _normalize_keyword_list(item.top_level_categories),
                    "mid": _normalize_keyword_list(item.mid_level_categories),
                    "low": _normalize_keyword_list(item.low_level_categories),
                },
                "themes": _normalize_keyword_list(item.themes),
                "journals": _normalize_keyword_list(item.journals),
                "sourceProfile": item.source_profile or {},
            }
        )

    papers = []
    for item in papers_qs:
        year = _year_from_dates(
            item.date_published_online, item.date_published_print, item.date_published
        )
        papers.append(
            {
                "id": str(item.id),
                "title": item.title or "",
                "doi": item.doi or "",
                "journal": item.journal or "",
                "authors": [author.name or f"{author.first_name or ''} {author.last_name or ''}".strip() for author in item.authors.all()],
                "year": year or 0,
                "abstract": item.abstract or "",
                "link": item.url or item.download_url or item.license_url or "",
                "citations": item.tc_count or 0,
                "publishedOnline": item.date_published_online.isoformat() if item.date_published_online else "",
                "publishedPrint": item.date_published_print.isoformat() if item.date_published_print else "",
                "aiKeywords": _normalize_keyword_list(item.keywords)
                or _normalize_keyword_list(item.ai_keywords)
                or _normalize_keyword_list(item.faculty_keywords),
                "categories": {
                    "top": _normalize_keyword_list(item.top_level_categories),
                    "mid": _normalize_keyword_list(item.mid_level_categories),
                    "low": _normalize_keyword_list(item.low_level_categories),
                },
                "facultyMembers": _normalize_keyword_list(item.faculty_members),
                "facultyAffiliations": item.faculty_affiliations or {},
                "sourceMetadata": item.source_metadata or {},
                "engagementMetrics": item.engagement_metrics or {},
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
    except RuntimeError as exc:
        return Response(
            {"results": [], "count": 0, "detail": str(exc)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception as exc:
        return Response(
            {"results": [], "count": 0, "detail": f"Embedding failed: {exc}"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

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
                "link": paper.url or paper.download_url or paper.license_url or "",
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
                "categories": {
                    "top": _normalize_keyword_list(paper.top_level_categories),
                    "mid": _normalize_keyword_list(paper.mid_level_categories),
                    "low": _normalize_keyword_list(paper.low_level_categories),
                },
                "facultyMembers": _normalize_keyword_list(paper.faculty_members),
                "facultyAffiliations": paper.faculty_affiliations or {},
                "sourceMetadata": paper.source_metadata or {},
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
