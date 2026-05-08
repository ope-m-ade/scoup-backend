# SCOUP Search Engine

Last updated: May 8, 2026

The older IDF-based search design has been replaced by the current evidence-based backend search engine in:

```text
academic/search_engine.py
```

The current full report is maintained in the frontend repository:

```text
scoup-frontend-2.0/docs/search-engine-contract.md
```

## Current Backend Entry Points

| File | Role |
|---|---|
| `academic/views.py` | `unified_search()` exposes `GET /api/search/?q=<query>` and delegates to `run_search()`. |
| `academic/search_engine.py` | Main evidence-based ranking engine for faculty, papers, patents, and projects. |
| `academic/semantic.py` | OpenAI embedding helpers and cosine similarity. |
| `academic/ai_keywords.py` | OpenAI-powered faculty AI keyword generation. |
| `academic/management/commands/embed_papers.py` | Generates paper embeddings. |
| `academic/management/commands/generate_ai_keywords.py` | Generates faculty `ai_keywords`. |

## Current Design Summary

Search now ranks results by structured evidence:

- exact faculty name
- exact paper, patent, or project title
- fuzzy name/title
- faculty-entered keyword
- AI/recommended keyword
- paper author
- department
- imported paper raw category
- imported faculty raw keyword/category
- abstract/description
- semantic embedding similarity

Every result includes:

```json
{
  "confidence": 95,
  "aiJustification": "Recommended research keyword matches your search: 'Machine Learning Challenges'.",
  "matchEvidence": {
    "match_source": "AI keyword",
    "match_strength": "phrase",
    "matched_value": "Machine Learning Challenges",
    "matched_terms": ["Machine Learning Challenges"],
    "score": 95
  }
}
```

The central rule is:

> Lexical evidence wins first. Embeddings are an additional discovery layer, not a replacement for exact title, name, author, or keyword matches.

Current broad-discipline calibration:

- Exact faculty department phrase matches score `90`.
- Imported paper category phrase matches score `82`.
- Imported faculty raw keyword/category phrase matches score `78`.
- After confidence sorting, a diversification pass interleaves result types so broad searches do not show a long block of only faculty or only papers.

For the complete score table, frontend behavior, suggestion behavior, known tradeoffs, and representative queries, see:

```text
../scoup-frontend-2.0/docs/search-engine-contract.md
```
