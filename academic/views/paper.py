import json
import os
import re
import uuid

import pdfplumber
import requests as http_requests
from openai import OpenAI
from rest_framework import filters, generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Faculty, Paper, Patent, Project
from ..serializers import PaperSerializer, PatentSerializer, ProjectSerializer
from .utils import (
    _normalize_keyword_list,
    _clean_abstract,
    _title_similarity,
    _get_request_faculty,
)

# Matches bare DOIs and doi.org URL prefixes
_DOI_URL_PREFIX = re.compile(r"https?://(dx\.)?doi\.org/", re.IGNORECASE)


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


class PaperDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PaperSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Paper.objects.none()
        return Paper.objects.filter(authors=faculty)


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


class ProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Project.objects.none()
        return Project.objects.filter(faculty=faculty)


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


class PatentDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PatentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        faculty = _get_request_faculty(self.request.user)
        if not faculty:
            return Patent.objects.none()
        return Patent.objects.filter(faculty=faculty)


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

        # Validate file type and size (max 10 MB)
        if hasattr(file, "content_type") and file.content_type != "application/pdf":
            return Response({"error": "Only PDF files are accepted."}, status=400)
        if hasattr(file, "size") and file.size > 10 * 1024 * 1024:
            return Response({"error": "CV must be under 10 MB."}, status=400)

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

        # ── 3. Keep upload response fast ────────────────────────────────────
        # Live abstract enrichment can make several external API calls per paper
        # and has caused Render/Gunicorn worker timeouts for publication-heavy CVs.
        # Keep this request focused on extraction; optional enrichment can be
        # re-enabled for a small sample via env var during testing.
        enrich_abstracts = os.environ.get("CV_UPLOAD_ENRICH_ABSTRACTS", "False") == "True"
        max_enriched = int(os.environ.get("CV_UPLOAD_MAX_ABSTRACT_ENRICH", "3"))
        for index, paper in enumerate(papers):
            if enrich_abstracts and index < max_enriched:
                title = paper.get("title", "")
                doi = paper.get("doi")
                paper["abstract"] = fetch_abstract(title, doi) or ""
            else:
                paper["abstract"] = ""

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
        # "all_draft" now covers both draft and in-review (all unpublished)
        qs = Paper.objects.filter(
            authors=faculty,
            status__in=[Paper.STATUS_DRAFT, Paper.STATUS_IN_REVIEW],
        )
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
