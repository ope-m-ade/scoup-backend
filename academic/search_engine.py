"""
SCOUP unified backend search.

Design rule:
  Lexical evidence wins first. Embeddings are an additional discovery layer,
  not a replacement for exact title/name/keyword matches.

Score contract used for every result type:
  99  exact title/name match
  97  exact phrase in title/name
  95  exact phrase in faculty-entered or AI-generated keyword
  90  all query terms in title/name
  88  close fuzzy title/name match
  88  exact paper author match
  90  exact phrase in department
  78  exact phrase in imported faculty keyword
  82  exact phrase in imported paper category keyword
  78  exact phrase in abstract/description
  62  all query terms in department
  60  all query terms in abstract/description/department
  40-85 semantic embedding match
  <40 filtered out

For queries that match an existing department name, faculty in that department
receive a small boost and faculty outside that department receive a small
penalty. This keeps "computer science" style searches from being dominated by
keyword-only matches from unrelated departments.
"""

import ast
import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Iterable

from django.db import OperationalError

from .models import Department, Faculty, Paper, Patent, Project
from .semantic import cosine_similarity, create_query_embedding


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "as", "be", "was",
    "are", "were", "have", "has", "do", "does", "did", "not", "no",
    "so", "that", "this", "what", "who", "how", "can", "could", "would",
    "will", "may", "might", "some", "any", "all", "more", "than", "me",
    "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "find", "get", "give", "look", "looking", "about", "into", "show",
    "want", "need", "use", "using", "used", "work", "working", "works",
}

MIN_SCORE = 40
SEMANTIC_THRESHOLD = 0.22
DEPARTMENT_QUERY_MATCH_BONUS = 5
DEPARTMENT_QUERY_MISMATCH_PENALTY = 8

# Query expansion — maps common abbreviations and informal terms to their
# full NSF taxonomy equivalents so search works even without exact wording.
QUERY_EXPANSIONS = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "llm": "large language models",
    "llms": "large language models",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "dl": "deep learning",
    "nn": "neural networks",
    "cs": "computer science",
    "it": "information technology",
    "db": "database",
    "hci": "human computer interaction",
    "iot": "internet of things",
    "ar": "augmented reality",
    "vr": "virtual reality",
    "xr": "extended reality",
    "ui": "user interface",
    "ux": "user experience",
    "devops": "software engineering",
    "cybersecurity": "computer security",
    "infosec": "computer security",
    "bioinformatics": "bioinformatics",
    "stats": "statistics",
    "econ": "economics",
    "psych": "psychology",
    "bio": "biology",
    "chem": "chemistry",
    "phys": "physics",
    "math": "mathematics",
    "calc": "mathematics",
    "geo": "geography",
    "env": "environmental science",
    "climate": "environmental science",
    "neurosci": "neuroscience",
    "pharma": "pharmacology",
    "prompt engineering": "artificial intelligence",
    "generative ai": "artificial intelligence",
    "chatgpt": "artificial intelligence",
    "gpt": "artificial intelligence",
    "transformer": "artificial intelligence",
    "data science": "statistics",
    "big data": "computer and information sciences",
}
MAX_CONSECUTIVE_RESULT_TYPE = 3
RESULT_DIVERSITY_SCORE_WINDOW = 25


@dataclass(frozen=True)
class Evidence:
    score: int
    kind: str
    field: str
    value: str
    matched: tuple[str, ...]


def normalize_text(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()


def expand_query(query: str) -> str:
    """Expand abbreviations and informal terms to their full taxonomy equivalents."""
    normalized = query.strip().lower()
    # Check full query first (e.g. "prompt engineering")
    if normalized in QUERY_EXPANSIONS:
        return query + " " + QUERY_EXPANSIONS[normalized]
    # Then check individual words
    words = normalized.split()
    extras = []
    for word in words:
        if word in QUERY_EXPANSIONS:
            extras.append(QUERY_EXPANSIONS[word])
    if extras:
        return query + " " + " ".join(extras)
    return query


def parse_query(query: str) -> tuple[str, list[str]]:
    expanded = expand_query(query)
    phrase = normalize_text(expanded)
    # Use the original query words (not the expanded form) for "all terms must match"
    # checks — this prevents expansion tokens like "artificial intelligence" from making
    # a search for "AI" fail against a project that literally contains "AI".
    original_words = [
        w for w in normalize_text(query).split()
        if len(w) >= 2 and w not in STOPWORDS
    ]
    return phrase, original_words


def text_similarity(left, right) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def normalize_keyword_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = ast.literal_eval(stripped)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def merge_unique_list(*values) -> list[str]:
    ordered = []
    seen = set()
    for value in values:
        for item in normalize_keyword_list(value):
            key = normalize_text(item)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    return ordered


def year_from_dates(*dates):
    for dt in dates:
        if dt:
            return dt.year
    return None


def normalize_paper_link(download_url, url, license_url, doi):
    for value in (download_url, url, license_url):
        clean = re.sub(r"\s+", "", str(value or "").strip())
        if clean.startswith(("http://", "https://")):
            return clean
    clean_doi = re.sub(r"\s+", "", str(doi or "").strip())
    if clean_doi and clean_doi.count("/") >= 1:
        prefix, suffix = clean_doi.split("/", 1)
        if prefix and len(suffix) > 3:
            return f"https://doi.org/{clean_doi}"
    return ""


def faculty_school_names(faculty) -> list[str]:
    names = []
    if getattr(faculty, "primary_school_id", None):
        names.append(faculty.primary_school.name)
    names.extend([school.name for school in faculty.schools.all()])
    return merge_unique_list(names)


def faculty_department_names(faculty) -> list[str]:
    names = []
    if getattr(faculty, "primary_department_id", None):
        names.append(faculty.primary_department.name)
    names.extend([department.name for department in faculty.departments.all()])
    return merge_unique_list(names)


def department_matches_query(department_names: list[str], query_phrase: str, words: list[str]) -> bool:
    for department_name in department_names:
        if phrase_in_text(query_phrase, department_name) or words_in_text(words, department_name):
            return True
    return False


def query_targets_department(raw_query: str, words: list[str]) -> bool:
    query_phrase = normalize_text(raw_query)
    if len(query_phrase) < 3:
        return False

    for department in Department.objects.filter(is_active=True).only("name"):
        department_name = department.name or ""
        department_norm = normalize_text(department_name)
        if not department_norm:
            continue
        if phrase_in_text(query_phrase, department_name) or words_in_text(words, department_name):
            return True
        if len(query_phrase) >= 8 and text_similarity(query_phrase, department_norm) >= 0.9:
            return True
    return False


def adjust_faculty_department_query_evidence(
    evidence: Evidence,
    department_names: list[str],
    query_phrase: str,
    words: list[str],
    is_department_query: bool,
) -> Evidence:
    if not is_department_query:
        return evidence

    if department_matches_query(department_names, query_phrase, words):
        return replace(
            evidence,
            score=min(99, evidence.score + DEPARTMENT_QUERY_MATCH_BONUS),
        )

    if evidence.field == "name":
        return evidence

    return replace(
        evidence,
        score=max(MIN_SCORE, evidence.score - DEPARTMENT_QUERY_MISMATCH_PENALTY),
    )


def words_in_text(words: Iterable[str], text: str) -> bool:
    text_words = set(normalize_text(text).split())
    return bool(text_words) and all(word in text_words for word in words)


def phrase_in_text(phrase: str, text: str) -> bool:
    phrase_words = normalize_text(phrase).split()
    text_words = normalize_text(text).split()
    if not phrase_words or len(phrase_words) > len(text_words):
        return False
    phrase_len = len(phrase_words)
    return any(
        text_words[index:index + phrase_len] == phrase_words
        for index in range(0, len(text_words) - phrase_len + 1)
    )


def coverage(words: list[str], text: str) -> float:
    if not words:
        return 0.0
    text_words = set(normalize_text(text).split())
    return sum(1 for word in words if word in text_words) / len(words)


def evidence_strength(kind: str) -> str:
    if kind == "exact":
        return "exact"
    if kind in {"phrase", "keyword_phrase", "context_phrase"}:
        return "phrase"
    if kind in {"all_terms", "keyword_terms", "context_terms"}:
        return "all_terms"
    if kind == "close":
        return "fuzzy"
    if kind == "semantic":
        return "semantic"
    return "partial"


def evidence_payload(evidence: Evidence) -> dict:
    return {
        "match_source": evidence.field,
        "match_strength": evidence_strength(evidence.kind),
        "matched_value": evidence.value,
        "matched_terms": list(evidence.matched),
        "score": evidence.score,
    }


def likely_metadata_text(text: str) -> bool:
    normalized = normalize_text(text)
    metadata_markers = (
        "authors",
        "published in",
        "index terms",
        "acknowledgements",
        "department",
        "university",
        "school of",
        "college of",
        "faculty of",
        "corresponding author",
        "affiliation",
        "contributor",
        "proceedings editor",
        "program chairs",
        "funding",
    )
    return any(marker in normalized for marker in metadata_markers)


def searchable_paper_abstract(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    abstract_match = re.search(r"\*\*abstract:\*\*\s*", raw, flags=re.IGNORECASE)
    if not abstract_match:
        return raw

    abstract_start = abstract_match.end()
    next_section = re.search(r"\n\s*\*\*[^*]+:\*\*", raw[abstract_start:], flags=re.IGNORECASE)
    if next_section:
        return raw[abstract_start:abstract_start + next_section.start()].strip()
    return raw[abstract_start:].strip()


def best_evidence(
    *,
    phrase: str,
    words: list[str],
    primary_fields: list[tuple[str, str]],
    keyword_fields: list[tuple[str, list[str], int]],
    context_fields: list[tuple[str, str, int]],
) -> Evidence | None:
    candidates: list[Evidence] = []

    for field, value in primary_fields:
        value_norm = normalize_text(value)
        if not value_norm:
            continue
        similarity = text_similarity(phrase, value_norm)
        if phrase == value_norm:
            candidates.append(Evidence(99, "exact", field, value, (value,)))
        elif phrase_in_text(phrase, value):
            candidates.append(Evidence(97, "phrase", field, value, (value,)))
        elif len(phrase) >= 8 and similarity >= 0.90:
            candidates.append(Evidence(94, "close", field, value, (value,)))
        elif len(phrase) >= 8 and similarity >= 0.82:
            candidates.append(Evidence(88, "close", field, value, (value,)))
        elif words_in_text(words, value):
            candidates.append(Evidence(90, "all_terms", field, value, tuple(words)))

    for field, values, phrase_score in keyword_fields:
        for value in values:
            if phrase_in_text(phrase, value):
                candidates.append(Evidence(phrase_score, "keyword_phrase", field, value, (value,)))
            elif field == "author" and len(phrase) >= 6 and text_similarity(phrase, value) >= 0.88:
                candidates.append(Evidence(phrase_score, "keyword_phrase", field, value, (value,)))
            elif words_in_text(words, value):
                candidates.append(Evidence(max(MIN_SCORE, phrase_score - 7), "keyword_terms", field, value, (value,)))

    for field, value, phrase_score in context_fields:
        effective_phrase_score = phrase_score
        if field == "abstract" and likely_metadata_text(value):
            effective_phrase_score = min(58, phrase_score)
        if phrase_in_text(phrase, value):
            candidates.append(Evidence(effective_phrase_score, "context_phrase", field, value, ()))
        elif words_in_text(words, value):
            candidates.append(Evidence(min(65, effective_phrase_score - 13), "context_terms", field, value, ()))
        else:
            cov = coverage(words, value)
            if cov >= 0.67 and len(words) >= 3:
                candidates.append(Evidence(52, "partial_context", field, value, ()))
            elif cov > 0 and len(words) == 1:
                candidates.append(Evidence(45, "partial_context", field, value, ()))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item.score)


def evidence_justification(entity: str, evidence: Evidence) -> str:
    if evidence.kind == "semantic":
        return "Semantic match based on the paper's embedded research content."
    if evidence.kind == "exact":
        if evidence.field == "name":
            return "Exact faculty name match."
        if evidence.field == "title":
            return f"Exact {entity} title match."
        return f"Exact match in {evidence.field}."
    if evidence.kind == "phrase":
        if evidence.field == "title":
            return f"The {entity} title directly contains your search phrase."
        return f"{evidence.field.capitalize()} directly contains your search phrase."
    if evidence.kind == "close":
        if evidence.field == "name":
            return "Close faculty name match, allowing for spacing or spelling differences."
        if evidence.field == "title":
            return f"Close {entity} title match, allowing for minor spelling differences."
        return f"Close match in {evidence.field}."
    if evidence.kind == "all_terms":
        if evidence.field == "title":
            return f"The {entity} title contains all meaningful search terms."
        return f"{evidence.field.capitalize()} contains all meaningful search terms."
    if evidence.kind == "keyword_phrase":
        if evidence.field == "author":
            return f"Paper author matches your search: '{evidence.value}'."
        if evidence.field == "faculty keyword":
            return f"Faculty-entered keyword matches your search: '{evidence.value}'."
        if evidence.field == "AI keyword":
            return f"Recommended research keyword matches your search: '{evidence.value}'."
        if evidence.field == "raw keyword":
            return f"Imported faculty keyword/category matches your search: '{evidence.value}'."
        if evidence.field == "paper AI keyword":
            return f"Generated paper keyword matches your search: '{evidence.value}'."
        if evidence.field == "paper faculty keyword":
            return f"Faculty-provided paper keyword matches your search: '{evidence.value}'."
        if evidence.field == "paper raw keyword":
            return f"Paper is categorized under '{evidence.value}'."
        if evidence.field == "patent keyword":
            return f"Patent keyword matches your search: '{evidence.value}'."
        if evidence.field == "project keyword":
            return f"Project keyword matches your search: '{evidence.value}'."
        return f"Keyword matches your search: '{evidence.value}'."
    if evidence.kind == "keyword_terms":
        if evidence.field == "author":
            return f"Paper author contains all meaningful search terms: '{evidence.value}'."
        return f"Keyword contains all meaningful search terms: '{evidence.value}'."
    if evidence.kind == "context_phrase":
        if evidence.field == "department":
            return "Department affiliation directly matches your search phrase."
        if evidence.field == "abstract":
            return "Paper abstract directly contains your search phrase."
        if evidence.field == "bio":
            return "Faculty bio directly contains your search phrase."
        if evidence.field == "description":
            return f"{entity.capitalize()} description directly contains your search phrase."
        return f"{evidence.field.capitalize()} directly contains your search phrase."
    if evidence.kind == "context_terms":
        if evidence.field == "department":
            return "Department affiliation contains all meaningful search terms."
        if evidence.field == "abstract":
            return "Paper abstract contains all meaningful search terms."
        if evidence.field == "bio":
            return "Faculty bio contains all meaningful search terms."
        if evidence.field == "description":
            return f"{entity.capitalize()} description contains all meaningful search terms."
        return f"{evidence.field.capitalize()} contains all meaningful search terms."
    return f"{entity.capitalize()} contains related search terms."


def faculty_result(faculty, evidence: Evidence, request) -> dict:
    dept_names = faculty_department_names(faculty)
    school_names = faculty_school_names(faculty)
    display_keywords = merge_unique_list(
        faculty.faculty_keywords,
        faculty.ai_keywords,
        faculty.themes,
        faculty.keywords,
    )[:12]
    name = (
        faculty.name
        or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        or faculty.email
        or ""
    )
    return {
        "type": "faculty",
        "confidence": evidence.score,
        "aiJustification": evidence_justification("faculty", evidence),
        "matchEvidence": evidence_payload(evidence),
        "matchedKeywords": list(evidence.matched)[:8],
        "data": {
            "id": str(faculty.id),
            "name": name,
            "title": faculty.title or "",
            "department": dept_names[0] if dept_names else "Unassigned",
            "departmentAffiliations": dept_names,
            "schoolAffiliations": school_names,
            "email": faculty.email or "",
            "phone": faculty.phone or "",
            "photo": request.build_absolute_uri(faculty.photo.url) if faculty.photo else "",
            "bio": faculty.bio or "",
            "researchInterests": display_keywords,
            "aiKeywords": display_keywords,
            "metricsProfile": {
                "totalCitations": faculty.total_citations or 0,
                "articleCount": faculty.article_count or 0,
                "averageCitations": float(faculty.average_citations or 0),
            },
            "themes": normalize_keyword_list(faculty.themes),
            "journals": normalize_keyword_list(faculty.journals),
            "nsfCategories": normalize_keyword_list(faculty.keywords)[:6],
        },
    }


def paper_result(paper, evidence: Evidence) -> dict:
    year = year_from_dates(
        paper.date_published_online, paper.date_published_print, paper.date_published
    )
    keywords = merge_unique_list(paper.keywords, paper.ai_keywords, paper.faculty_keywords)
    return {
        "type": "paper",
        "confidence": evidence.score,
        "aiJustification": evidence_justification("paper", evidence),
        "matchEvidence": evidence_payload(evidence),
        "matchedKeywords": list(evidence.matched)[:8],
        "data": {
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
            "link": normalize_paper_link(paper.download_url, paper.url, paper.license_url, paper.doi),
            "aiKeywords": keywords,
            "citations": paper.tc_count or 0,
            "semanticScore": float(getattr(paper, "_semantic_score", 0.0)),
        },
    }


def patent_result(patent, evidence: Evidence) -> dict:
    inventors = [
        faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        for faculty in patent.faculty.all()
    ]
    issue_year = patent.issue_date.year if patent.issue_date else (
        patent.filing_date.year if patent.filing_date else None
    )
    keywords = normalize_keyword_list(patent.aiKeywords)
    return {
        "type": "patent",
        "confidence": evidence.score,
        "aiJustification": evidence_justification("patent", evidence),
        "matchEvidence": evidence_payload(evidence),
        "matchedKeywords": list(evidence.matched)[:8],
        "data": {
            "id": str(patent.id),
            "title": patent.title or "",
            "patentNumber": patent.patent_number or "",
            "inventors": inventors,
            "year": issue_year or 0,
            "description": patent.abstract or "",
            "link": patent.link or "",
            "aiKeywords": keywords,
        },
    }


def project_result(project, evidence: Evidence) -> dict:
    lead_faculty = [
        faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        for faculty in project.faculty.all()
    ]
    keywords = normalize_keyword_list(project.keywords)
    return {
        "type": "project",
        "confidence": evidence.score,
        "aiJustification": evidence_justification("project", evidence),
        "matchEvidence": evidence_payload(evidence),
        "matchedKeywords": list(evidence.matched)[:8],
        "data": {
            "id": str(project.id),
            "title": project.title or "",
            "status": project.status or "Unknown",
            "leadFaculty": lead_faculty,
            "startDate": str(project.start_date) if project.start_date else "",
            "endDate": str(project.end_date) if project.end_date else "",
            "description": project.description or "",
            "link": project.link or "",
            "aiKeywords": keywords,
            "fundingSource": project.funding_source or "",
            "isOpenToCollaboration": project.is_open_to_collaboration,
            "collaborationInvitation": project.collaboration_invitation or "",
            "allowStudentInterest": project.allow_student_interest,
        },
    }


def score_faculty(phrase: str, words: list[str], request, is_department_query: bool = False) -> list[dict]:
    results = []
    faculty_qs = (
        Faculty.objects
        .filter(confirmed_su_faculty=True) | Faculty.objects.filter(is_approved=True)
    ).exclude(user__is_staff=True).exclude(user__is_superuser=True).select_related("primary_department", "primary_school").prefetch_related("departments", "schools")

    for faculty in faculty_qs.distinct():
        dept_names = faculty_department_names(faculty)
        name = (
            faculty.name
            or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
            or faculty.email
            or ""
        )
        evidence = best_evidence(
            phrase=phrase,
            words=words,
            primary_fields=[("name", name)],
            keyword_fields=[
                ("faculty keyword", normalize_keyword_list(faculty.faculty_keywords), 95),
                ("AI keyword", merge_unique_list(faculty.ai_keywords, faculty.themes), 95),
                ("raw keyword", normalize_keyword_list(faculty.keywords), 78),
            ],
            context_fields=[
                ("department", " ".join(dept_names), 90),
                ("bio", faculty.bio or "", 65),
            ],
        )
        if evidence and evidence.score >= MIN_SCORE:
            evidence = adjust_faculty_department_query_evidence(
                evidence,
                dept_names,
                phrase,
                words,
                is_department_query,
            )
            results.append(faculty_result(faculty, evidence, request))
    return results


def score_papers_lexical(phrase: str, words: list[str]) -> dict[int, tuple[Paper, Evidence]]:
    scored = {}
    papers = Paper.objects.prefetch_related("authors").all()
    for paper in papers:
        author_names = merge_unique_list(
            [
                author.name or f"{author.first_name or ''} {author.last_name or ''}".strip()
                for author in paper.authors.all()
            ],
            paper.faculty_members,
        )
        evidence = best_evidence(
            phrase=phrase,
            words=words,
            primary_fields=[("title", paper.title or "")],
            keyword_fields=[
                ("author", author_names, 88),
                ("paper AI keyword", normalize_keyword_list(paper.ai_keywords), 92),
                ("paper faculty keyword", normalize_keyword_list(paper.faculty_keywords), 92),
                ("paper raw keyword", normalize_keyword_list(paper.keywords), 82),
            ],
            context_fields=[
                ("abstract", searchable_paper_abstract(paper.abstract), 78),
                ("journal", paper.journal or "", 60),
            ],
        )
        if evidence and evidence.score >= MIN_SCORE:
            scored[paper.id] = (paper, evidence)
    return scored


def score_papers_semantic(query: str) -> dict[int, tuple[Paper, Evidence]]:
    try:
        query_embedding = create_query_embedding(query)
    except Exception:
        return {}
    if not query_embedding:
        return {}

    scored = {}
    try:
        papers = (
            Paper.objects
            .exclude(paper_embedding=[])
            .exclude(paper_embedding__isnull=True)
            .prefetch_related("authors")
        )
        for paper in papers:
            similarity = cosine_similarity(query_embedding, paper.paper_embedding or [])
            if similarity < SEMANTIC_THRESHOLD:
                continue
            confidence = min(85, max(40, round((similarity - 0.15) / 0.35 * 100)))
            paper._semantic_score = round(similarity * 100, 1)
            scored[paper.id] = (
                paper,
                Evidence(confidence, "semantic", "content", paper.title or "", (query,)),
            )
    except OperationalError:
        return {}
    return scored


def score_papers(query: str, phrase: str, words: list[str]) -> list[dict]:
    by_id = score_papers_lexical(phrase, words)
    for paper_id, (paper, semantic_evidence) in score_papers_semantic(query).items():
        existing = by_id.get(paper_id)
        if not existing or semantic_evidence.score > existing[1].score:
            by_id[paper_id] = (paper, semantic_evidence)

    return [
        paper_result(paper, evidence)
        for paper, evidence in by_id.values()
        if evidence.score >= MIN_SCORE
    ]


def score_patents(phrase: str, words: list[str]) -> list[dict]:
    results = []
    for patent in Patent.objects.prefetch_related("faculty").all():
        evidence = best_evidence(
            phrase=phrase,
            words=words,
            primary_fields=[("title", patent.title or "")],
            keyword_fields=[("patent keyword", normalize_keyword_list(patent.aiKeywords), 95)],
            context_fields=[("description", patent.abstract or "", 78)],
        )
        if evidence and evidence.score >= MIN_SCORE:
            results.append(patent_result(patent, evidence))
    return results


def score_projects(phrase: str, words: list[str]) -> list[dict]:
    results = []
    for project in Project.objects.prefetch_related("faculty").all():
        evidence = best_evidence(
            phrase=phrase,
            words=words,
            primary_fields=[("title", project.title or "")],
            keyword_fields=[("project keyword", normalize_keyword_list(project.keywords), 95)],
            context_fields=[("description", project.description or "", 78)],
        )
        if evidence and evidence.score >= MIN_SCORE:
            results.append(project_result(project, evidence))
    return results


def diversify_results(results: list[dict]) -> list[dict]:
    """
    Keep relevance as the primary signal, but prevent broad searches from showing
    a long run of one result type when another type has a reasonably close score.
    """
    remaining = list(results)
    diversified = []

    while remaining:
        candidate_index = 0
        recent_types = [
            result["type"]
            for result in diversified[-MAX_CONSECUTIVE_RESULT_TYPE:]
        ]
        if (
            len(recent_types) == MAX_CONSECUTIVE_RESULT_TYPE
            and len(set(recent_types)) == 1
        ):
            repeated_type = recent_types[0]
            current_score = remaining[0]["confidence"]
            for index, result in enumerate(remaining[1:], start=1):
                if result["type"] == repeated_type:
                    continue
                if current_score - result["confidence"] <= RESULT_DIVERSITY_SCORE_WINDOW:
                    candidate_index = index
                    break

        diversified.append(remaining.pop(candidate_index))

    return diversified


def run_search(query: str, request) -> dict:
    query = query.strip()
    if len(query) < 2:
        return {"results": [], "count": 0, "query": query}

    phrase, words = parse_query(query)
    if not words:
        return {"results": [], "count": 0, "query": query}

    results = []
    results.extend(score_faculty(phrase, words, request, query_targets_department(query, words)))
    results.extend(score_papers(query, phrase, words))
    results.extend(score_patents(phrase, words))
    results.extend(score_projects(phrase, words))

    results.sort(
        key=lambda result: (
            result["confidence"],
            {"paper": 4, "faculty": 3, "patent": 2, "project": 1}.get(result["type"], 0),
        ),
        reverse=True,
    )
    results = diversify_results(results)
    return {"results": results[:250], "count": len(results), "query": query}
