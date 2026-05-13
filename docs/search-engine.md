# SCOUP Search Engine

Last updated: May 10, 2026

---

## Overview

SCOUP's search engine operates on two simultaneous layers — **lexical** and **semantic** — that run on every query and return whichever produces the highest confidence score per result.

The central design rule:

> Lexical evidence wins first. Embeddings are an additional discovery layer, not a replacement for exact title, name, author, or keyword matches.

---

## Key Files

| File                                                   | Role                                                                           |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ |
| `academic/search_engine.py`                            | Main ranking engine — lexical scoring, query expansion, result diversification |
| `academic/semantic.py`                                 | OpenAI embedding helpers and cosine similarity                                 |
| `academic/views.py`                                    | `unified_search()` exposes `GET /api/search/?q=<query>`                        |
| `academic/management/commands/embed_papers.py`         | Batch-generates paper embeddings via OpenAI                                    |
| `academic/management/commands/generate_ai_keywords.py` | Generates faculty AI keywords via OpenAI                                       |

---

## Layer 1 — Lexical Search

### Query Expansion

Before any matching occurs, the query is expanded using a hardcoded dictionary (`QUERY_EXPANSIONS`). This maps abbreviations and informal terms to their full NSF taxonomy equivalents:

| Input                | Expanded to                   |
| -------------------- | ----------------------------- |
| `AI`                 | `artificial intelligence`     |
| `LLM` / `LLMs`       | `large language models`       |
| `ML`                 | `machine learning`            |
| `NLP`                | `natural language processing` |
| `CV`                 | `computer vision`             |
| `prompt engineering` | `artificial intelligence`     |
| `ChatGPT` / `GPT`    | `artificial intelligence`     |
| `data science`       | `statistics`                  |
| `cybersecurity`      | `computer security`           |

This means searching "LLM" finds papers and faculty tagged with "large language models" even if the exact abbreviation never appears.

### Stopword Removal

Common noise words ("the", "and", "for", "in", "is", etc.) are stripped before matching so only meaningful terms are scored.

### Confidence Score Table

Every match is assigned a score based on where and how the match was found:

| Match type                                        | Score     |
| ------------------------------------------------- | --------- |
| Exact faculty name or paper title                 | 99        |
| Phrase found in title or name                     | 97        |
| Close fuzzy match on title or name (≥90% similar) | 94        |
| Close fuzzy match (≥82% similar)                  | 88        |
| All query terms in title or name                  | 90        |
| Exact phrase in faculty-entered keyword           | 95        |
| Exact phrase in AI-generated keyword              | 95        |
| Exact phrase in paper author name                 | 88        |
| Exact phrase in department                        | 90        |
| Exact phrase in imported paper category           | 82        |
| Exact phrase in imported faculty keyword          | 78        |
| Exact phrase in abstract or bio                   | 78        |
| All terms in abstract or department               | 60–65     |
| Semantic embedding match                          | 40–85     |
| Below threshold                                   | discarded |

Minimum score to appear in results: **40**

### Fields Searched Per Result Type

**Faculty:** name → faculty keywords → AI keywords → department → bio

**Papers:** title → AI keywords → faculty keywords → imported categories → authors → abstract → journal

**Patents:** title → keywords → abstract

**Projects:** title → keywords → description

---

## Layer 2 — Semantic Search

Semantic search finds results based on _meaning_, not just keyword presence. It requires an OpenAI API key.

### Generating Embeddings

Run the management command to generate embeddings for all papers:

```bash
python manage.py embed_papers
```

Options:

- `--force` — re-embed papers that already have embeddings
- `--batch-size N` — papers per API request (default: 50)
- `--limit N` — embed at most N papers
- `--dry-run` — report counts without writing

This should be re-run after every Academic Metrics import to cover newly added papers.

### How Semantic Scoring Works

1.  The user's query is converted to a vector (1536 numbers) via OpenAI in real time
2.  That vector is compared against each paper's stored `paper_embedding` using cosine similarity
3.  Papers with similarity ≥ 0.22 are surfaced as semantic matches
4.  Confidence score formula: `min(85, max(40, round((similarity - 0.15) / 0.35 * 100)))`
5.  Semantic scores are capped at **85** — an exact keyword match always outranks a semantic match

### Blending Lexical and Semantic

For each paper, both layers run. The higher score wins:

- Found by both → higher score used
- Found by lexical only → lexical score (max 99)
- Found by semantic only → semantic score (max 85)
- Found by neither → not returned

---

## Result Diversification

After scoring, results are sorted by confidence descending. A diversification pass then prevents more than 3 consecutive results of the same type. If the same type repeats 3+ times and the next different type's score is within 25 points, it is interleaved. This ensures broad searches don't show a wall of papers before any faculty appear.

---

## "Did You Mean?" Spell Correction

When a search returns fewer than 3 results OR all results score below 70% confidence, the frontend checks whether the query contains misspelled words.

**Mechanism:**

- A word pool is built from every paper title, faculty name, and keyword in the database
- Each word in the query is compared against the pool using Levenshtein (edit) distance
- If a known word is within 2 edits of a query word, it is suggested as a correction
- The corrected query is shown as a clickable link that re-runs the search

**Example:** `comporter` → suggests `computer` (2 edits away)

This is fully automatic — the word pool updates as new content is added to the database.

---

## What Requires the OpenAI API Key

| Feature                                     | Requires Key            |
| ------------------------------------------- | ----------------------- |
| Lexical search                              | ❌ No                   |
| Query expansion                             | ❌ No (hardcoded)       |
| "AI justification" text in results          | ❌ No (hardcoded)       |
| "Did you mean?" correction                  | ❌ No (local word pool) |
| Semantic search (real-time query embedding) | ✅ Yes                  |
| Paper embedding generation (`embed_papers`) | ✅ Yes                  |
| CV upload → paper extraction                | ✅ Yes                  |
| Generate faculty bio                        | ✅ Yes                  |
| Generate research interests                 | ✅ Yes                  |
| Generate paper keywords                     | ✅ Yes                  |

The system degrades gracefully without a key — lexical search continues to operate fully.

---

## Sample API Response

```json
{
  "confidence": 95,
  "aiJustification": "Recommended research keyword matches your search: 'Machine Learning'.",
  "matchEvidence": {
    "match_source": "AI keyword",
    "match_strength": "phrase",
    "matched_value": "Machine Learning",
    "matched_terms": ["Machine Learning"],
    "score": 95
  }
}
```
