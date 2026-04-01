import math
import os
from typing import Iterable, List


def _to_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def build_paper_semantic_text(paper) -> str:
    """Build stable semantic text from a Paper model instance."""
    parts = []

    if paper.title:
        parts.append(f"Title: {paper.title}")
    if paper.abstract:
        parts.append(f"Abstract: {paper.abstract}")
    if paper.journal:
        parts.append(f"Journal: {paper.journal}")

    ai_keywords = _to_list(getattr(paper, "keywords", None)) or _to_list(
        getattr(paper, "ai_keywords", None)
    )
    if ai_keywords:
        parts.append("Keywords: " + ", ".join(ai_keywords))

    themes = _to_list(getattr(paper, "themes", None))
    if themes:
        parts.append("Themes: " + ", ".join(themes))

    top = _to_list(getattr(paper, "top_level_categories", None))
    mid = _to_list(getattr(paper, "mid_level_categories", None))
    low = _to_list(getattr(paper, "low_level_categories", None))
    cat_parts = []
    if top:
        cat_parts.append("top=" + ", ".join(top))
    if mid:
        cat_parts.append("mid=" + ", ".join(mid))
    if low:
        cat_parts.append("low=" + ", ".join(low))
    if cat_parts:
        parts.append("Categories: " + " | ".join(cat_parts))

    authors = []
    try:
        authors = [
            author.name
            or f"{author.first_name or ''} {author.last_name or ''}".strip()
            for author in paper.authors.all()
        ]
    except Exception:
        authors = _to_list(getattr(paper, "faculty_members", None))

    authors = [a for a in authors if a]
    if authors:
        parts.append("Authors: " + ", ".join(authors))

    return "\n".join(parts).strip()


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError(
            "openai package not installed. Add it to requirements and install dependencies."
        ) from exc

    return OpenAI(api_key=api_key)


def create_embeddings(inputs: Iterable[str], model: str) -> List[List[float]]:
    values = [str(v or "").strip() for v in inputs]
    if not values:
        return []

    client = get_openai_client()
    response = client.embeddings.create(model=model, input=values)
    return [item.embedding for item in response.data]


def create_query_embedding(query: str, model: str) -> List[float]:
    values = create_embeddings([query], model=model)
    return values[0] if values else []


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) != len(vec_b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b

    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
