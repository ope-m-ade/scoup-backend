import json
import os
import re

from .models import Faculty
from .search_engine import merge_unique_list, normalize_keyword_list, searchable_paper_abstract
from .semantic import get_openai_client


DEFAULT_KEYWORD_MODEL = os.getenv("OPENAI_KEYWORD_MODEL", "gpt-4o-mini")


def _clean_text(value, limit=1200):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _faculty_name(faculty):
    return (
        faculty.name
        or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        or faculty.email
        or "Unnamed faculty"
    )


def _department_names(faculty):
    names = []
    if getattr(faculty, "primary_department_id", None):
        names.append(faculty.primary_department.name)
    names.extend([department.name for department in faculty.departments.all()])
    return merge_unique_list(names)


def _paper_lines(faculty, limit=12):
    papers = faculty.papers.all().order_by("-date_published", "-id")[:limit]
    lines = []
    for paper in papers:
        parts = []
        if paper.title:
            parts.append(f"Title: {_clean_text(paper.title, 300)}")
        abstract = searchable_paper_abstract(paper.abstract)
        if abstract:
            parts.append(f"Abstract: {_clean_text(abstract, 800)}")
        keywords = merge_unique_list(paper.ai_keywords, paper.faculty_keywords, paper.keywords)
        if keywords:
            parts.append("Existing paper keywords: " + ", ".join(keywords[:10]))
        if parts:
            lines.append(" | ".join(parts))
    return lines


def _patent_lines(faculty, limit=8):
    patents = faculty.patents.all().order_by("-issue_date", "-filing_date", "-id")[:limit]
    lines = []
    for patent in patents:
        parts = []
        if patent.title:
            parts.append(f"Title: {_clean_text(patent.title, 300)}")
        if patent.abstract:
            parts.append(f"Abstract: {_clean_text(patent.abstract, 700)}")
        keywords = normalize_keyword_list(patent.aiKeywords)
        if keywords:
            parts.append("Existing patent keywords: " + ", ".join(keywords[:10]))
        if parts:
            lines.append(" | ".join(parts))
    return lines


def _project_lines(faculty, limit=8):
    projects = faculty.projects.all().order_by("-start_date", "-id")[:limit]
    lines = []
    for project in projects:
        parts = []
        if project.title:
            parts.append(f"Title: {_clean_text(project.title, 300)}")
        if project.description:
            parts.append(f"Description: {_clean_text(project.description, 700)}")
        keywords = normalize_keyword_list(project.keywords)
        if keywords:
            parts.append("Existing project keywords: " + ", ".join(keywords[:10]))
        if parts:
            lines.append(" | ".join(parts))
    return lines


def build_faculty_keyword_context(faculty):
    current_keywords = merge_unique_list(
        faculty.faculty_keywords,
        faculty.ai_keywords,
        faculty.themes,
        faculty.keywords,
    )
    sections = [
        f"Faculty: {_faculty_name(faculty)}",
        f"Title: {_clean_text(faculty.title, 200)}",
        "Departments: " + ", ".join(_department_names(faculty)),
        f"Bio: {_clean_text(faculty.bio, 1000)}",
        "Current keywords/categories: " + ", ".join(current_keywords[:30]),
    ]

    papers = _paper_lines(faculty)
    if papers:
        sections.append("Papers:\n" + "\n".join(f"- {line}" for line in papers))

    patents = _patent_lines(faculty)
    if patents:
        sections.append("Patents:\n" + "\n".join(f"- {line}" for line in patents))

    projects = _project_lines(faculty)
    if projects:
        sections.append("Projects:\n" + "\n".join(f"- {line}" for line in projects))

    return "\n\n".join(section for section in sections if section.strip())


def has_keyword_generation_evidence(faculty):
    existing_keywords = merge_unique_list(
        faculty.faculty_keywords,
        faculty.ai_keywords,
        faculty.themes,
        faculty.keywords,
    )
    return (
        bool(_clean_text(faculty.bio, 80))
        or bool(existing_keywords)
        or faculty.papers.exists()
        or faculty.patents.exists()
        or faculty.projects.exists()
    )


def _parse_keyword_response(text):
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group(0))

    keywords = data.get("keywords", [])
    if not isinstance(keywords, list):
        return []
    return merge_unique_list(keywords)


def generate_faculty_ai_keywords(faculty, *, model=None, max_keywords=12):
    model = (model or DEFAULT_KEYWORD_MODEL).strip()
    max_keywords = max(3, min(int(max_keywords or 12), 20))
    context = build_faculty_keyword_context(faculty)

    client = get_openai_client()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate concise academic research keywords for faculty profiles. "
                    "Return only JSON with a 'keywords' array. Keywords should be specific, "
                    "search-friendly noun phrases. Avoid broad department labels unless the "
                    "faculty evidence clearly supports them."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Generate up to {max_keywords} research keywords for this faculty member. "
                    "Use their papers, patents, projects, bio, and existing categories as evidence. "
                    "Do not include explanations.\n\n"
                    f"{context}"
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else ""
    return _parse_keyword_response(content)[:max_keywords]


def faculty_queryset_for_keyword_generation():
    return (
        Faculty.objects.filter(profile_visibility=True)
        .filter(confirmed_su_faculty=True)
        .prefetch_related("papers", "patents", "projects", "departments")
        .select_related("primary_department")
        .order_by("last_name", "first_name", "id")
    )
