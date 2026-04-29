# SCOUP Search Engine — Architecture & Implementation Guide

> **Status:** Live and operational.  
> **Endpoint:** `GET /api/search/?q=<query>`  
> **Source files:**
> - Backend logic: `scoupdb/academic/views.py` → `unified_search()`
> - Embedding utilities: `scoupdb/academic/semantic.py`
> - Frontend wiring: `scoup-frontend-2.0/src/utils/searchEngine.ts`
> - Frontend data fetching: `scoup-frontend-2.0/src/utils/publicData.ts`

---

## What This Is

The SCOUP search engine is the **brain of the platform**. When a user types something like "machine learning healthcare" or "nursing education," the engine:

1. Understands what they actually mean (not just what they typed)
2. Weighs how rare or common each word is across the faculty database
3. Scores faculty against their curated research keywords — not a generic text blob
4. Finds semantically similar papers using pre-computed OpenAI embeddings
5. Guarantees exact title matches always surface, regardless of semantic score
6. Returns a ranked list with honest confidence percentages and human-readable explanations

The frontend is intentionally thin — it sends the query and renders the response. All intelligence lives in the backend.

---

## The 4-Layer Architecture

```
User types: "machine learning healthcare"
                        │
             ┌──────────▼──────────┐
             │  Layer 1: Parsing   │  Strip stopwords, extract real terms
             └──────────┬──────────┘
                        │  ["machine", "learning", "healthcare"]
             ┌──────────▼──────────┐
             │  Layer 2: IDF       │  Rare words = stronger signal
             └──────────┬──────────┘
                        │  machine→2.1, learning→1.9, healthcare→3.4
             ┌──────────▼──────────┐
             │  Layer 3: Faculty   │  Trust-level keyword scoring
             └──────────┬──────────┘
                        │  Dr. Smith: 78%, Dr. Jones: 45%...
             ┌──────────▼──────────┐
             │  Layer 4: Papers    │  Semantic + title-exact (both always run)
             └──────────┬──────────┘
                        │
             Ranked results with confidence + justification
```

---

## Layer 1 — Query Parsing

**Goal:** Extract meaningful search terms; discard linguistic noise.

The raw query string is lowercased and split into words. A word is kept only if:
- It is **3 or more characters** long
- It is **not in the stopword list**

### Stopword list
Common English function words that carry no research signal:

```
a, an, the, and, or, but, in, on, at, to, for, of, with, by, from,
is, it, its, as, be, was, are, were, have, has, do, does, did, not,
no, so, that, this, what, who, how, can, could, would, will, may,
might, some, any, all, more, than, me, my, we, our, you, your, he,
she, they, them, find, get, give, look, looking, about, into, show,
want, need, use, using, used, work, working, works
```

**Example:**
```
"looking for research about machine learning in healthcare"
→  terms:  ["research", "machine", "learning", "healthcare"]
   dropped: "looking", "for", "about", "in"
```

If zero terms remain after filtering (e.g., query was "in the"), the engine returns an empty result immediately.

---

## Layer 2 — Term Rarity (IDF Scoring)

**Goal:** Give rare, specific words more influence over common, generic ones.

IDF = Inverse Document Frequency. The rarer a word is across the faculty database, the more it tells us about the user's intent.

### Formula
```
IDF(word) = log( total_faculty / (1 + faculty_with_word) ) + 1
```

### Examples (with a database of ~367 faculty)
| Word | Faculty matches | IDF score | Meaning |
|---|---|---|---|
| `"biology"` | 60 / 367 | ≈ 1.8 | Common — weak signal |
| `"proteomics"` | 3 / 367 | ≈ 4.8 | Rare — strong signal |
| `"machine"` | 12 / 367 | ≈ 3.4 | Moderately specific |
| `"healthcare"` | 25 / 367 | ≈ 2.7 | Moderate |

IDF is computed **fresh per query** against the live database. The +1 floor ensures every word has a minimum contribution even if it matches no faculty (prevents zero-weight terms from being silently ignored).

Each query term gets its own IDF score stored in `word_idf`. This is used to multiply every scoring contribution in Layer 3.

---

## Layer 3 — Faculty Scoring (Trust-Level Keyword Matching)

**Goal:** Score each faculty member per query term using curated keywords, weighted by how trustworthy the keyword source is.

### The keyword sources (by trust level)

Faculty in the database have keywords from three different origins:

| Source | Field | Trust | Description |
|---|---|---|---|
| `themes` | Curated paper themes | **Highest** | Extracted from actual paper content by Academic Metrics. Specific and verified. Example: `"Molecular evolution"`, `"Human trafficking prevention"` |
| `faculty_keywords` | Faculty research interests | **High** | User-entered or admin-entered research interests. Specific but may vary in format. Example: `"Machine learning in sports science"` |
| `ai_keywords` | Academic Metrics categories | **Low** | Broad journal taxonomy assigned by Academic Metrics. Noisy and verbose. Example: `"Biochemistry, biophysics, and molecular biology"`. Only first 8 used. |

**Why this matters:** Early versions of the engine joined all keywords into a single text blob and searched for words anywhere in it. This caused "biology" to match faculty whose keyword #40 was "Biological and biomedical sciences" — giving them equal scores to faculty whose primary theme is "Biology." The trust-level approach fixes this.

### Scoring per keyword (the `_kw_score` function)

For each query word, the engine checks each keyword in a list individually:

```python
def _kw_score(kw_list, word, exact_pts, boundary_pts):
    for kw in kw_list:
        kw_lower = kw.lower()
        if kw_lower == word:
            return exact_pts          # "biology" == "biology"
        elif kw_lower.startswith(word + " ") or f" {word}" in kw_lower:
            return boundary_pts       # "biology" in "molecular biology"
    return 0
    # No substring match — too noisy ("bio" would match "biography")
```

**No raw substring matching.** A word must appear as a whole word (at the start, end, or separated by a space) to register. This prevents partial matches like "bio" matching "biography."

### Point values by source

| Source | Exact match | Boundary match |
|---|---|---|
| `themes` | **50 pts** | **30 pts** |
| `faculty_keywords` | **40 pts** | **22 pts** |
| `ai_keywords` (first 8 only) | **20 pts** | **10 pts** |
| `bio` text (fallback, word not found in keywords) | **6 pts** | — |
| Name match | **18 pts** | — |

### Weighted scoring formula

For each query word `w`:
```
word_score(w) = best_pts(w) × IDF(w)
```

Total raw score for a faculty member:
```
raw_score = Σ word_score(w) for all query words w
```

### Confidence normalization

```
max_possible = len(query_words) × 50 × max(IDF values)
confidence   = min(95, round((raw_score / max_possible) × 100))
```

The 95 ceiling prevents any result from showing 100% (which would imply perfect certainty).

**Threshold:** Results with `confidence < 30%` are dropped — they are too weak to be meaningful.

### Why confidence is honest

A faculty member who has "biology" as an exact theme gets `50 × IDF` points. One who only has "Evolutionary biology" gets `30 × IDF` (boundary match). One who only mentions biology in their bio text gets `6 × IDF`. The confidence scores therefore reflect real specificity — Dana L. Price at 95% genuinely works in Biology as her primary focus; Chelsea M Berns at 60% works in Evolutionary Biology, which contains biology but isn't her only focus.

---

## Layer 4 — Paper Search (Semantic + Title-Exact, Always Both)

**Goal:** Find papers that are conceptually relevant to the query. Guarantee that papers with exact title matches always appear, even if their semantic score is low.

### Two passes always run in parallel

The engine runs **both** passes for every query and merges the results. This is critical — early versions ran keyword search only as a fallback when semantic found nothing, which caused exact title matches to be silently dropped when semantic search returned other results.

```
Query
  ├── Semantic pass   → papers with embedding similarity ≥ 0.22
  ├── Keyword pass    → stage 1 (AND): all words in title  [no limit]
  │                  → stage 2 (OR):  any word in title/abstract  [limit 20]
  └── Merge          → MAX(semantic_conf, keyword_conf) per paper
```

### Semantic pass

Every paper in the database has a pre-computed **embedding** — a list of 1,536 numbers representing its meaning (title + abstract + keywords + authors) as a point in high-dimensional space. These are generated by OpenAI's `text-embedding-3-small` model and stored in the `paper_embedding` field.

When a user searches, the engine:
1. Calls OpenAI to embed the query (same model, same 1,536 dimensions)
2. Computes **cosine similarity** between the query vector and every paper's stored vector
3. Keeps papers above the 0.22 threshold

```
similarity = dot(query_vec, paper_vec) / (|query_vec| × |paper_vec|)
```

Range: 0.0 (unrelated) → 1.0 (identical meaning).

In practice for this dataset:
- `0.40+` → highly relevant
- `0.30–0.40` → meaningfully related
- `0.22–0.30` → borderline but valid
- Below `0.22` → dropped

The threshold is 0.22 (not the standard 0.35) because single-word queries naturally produce lower raw similarity scores. "Biology" peaks at ~0.32 against the best biology paper — a 0.35 threshold would silently return zero papers for any short query.

#### Confidence scaling for semantic results

```
confidence = min(95, max(40, round((sim - 0.15) / 0.35 × 100)))
```

| Similarity | Confidence |
|---|---|
| 0.22 | floored to **40%** |
| 0.30 | ≈ **43%** |
| 0.35 | ≈ **57%** |
| 0.42 | ≈ **77%** |
| 0.50+ | capped at **95%** |

### Keyword pass (two-stage)

Always runs, regardless of whether semantic found results.

**Stage 1 — AND query (title exact match):**
```python
# All query words must appear in the title
title_and_q = Q()
for word in words:
    title_and_q &= Q(title__icontains=word)
```
No result limit. Every paper whose title contains all query words is guaranteed to be included. This is what makes searching a specific paper title reliable — "NLP Analysis of Shakespearean Characters" will always surface even if it has no embedding or a low semantic score.

**Stage 2 — OR query (broader):**
```python
# Any query word in title or abstract
broad_q |= Q(title__icontains=word) | Q(abstract__icontains=word)
```
Limited to 20 additional papers (beyond stage 1). Supplements semantic results with keyword-matched papers not found semantically.

#### Confidence scoring for keyword results

```
title_hits  = number of query words found in the title
abstract_hits = number of query words found in the abstract
raw = (title_hits × 20) + (abstract_hits × 5)
max_raw = len(words) × 20
confidence = min(90, max(40, round((raw / max_raw) × 90)))

# Exact title match boost:
if all query words in title:
    confidence = max(confidence, 85)
```

An exact all-word title match always scores **85% minimum**.

### Merging the two passes

Papers that appear in both passes take the **higher** of the two confidence scores:

```python
# For papers in both semantic and keyword results:
final_conf = max(semantic_conf, keyword_conf)
```

This ensures:
- A paper with exact title match + low semantic similarity → gets 85% (keyword wins)
- A paper with high semantic similarity + partial title match → gets its semantic score (semantic wins)
- Papers only in semantic → use semantic score
- Papers only in keyword → use keyword score

### Paper justification text

| Condition | Justification shown |
|---|---|
| All query words in title | `"Title directly matches your search terms."` |
| Semantic match + keyword overlap | `"Semantically matched on Molecular biology, Oncology."` |
| Semantic match + journal known | `"Semantically relevant paper published in Nature Medicine."` |
| Semantic match only | `"Content is semantically similar to your query (score: 0.38)."` |
| Keyword match + keyword overlap | `"Keyword match on hepatitis, vaccination."` |
| Keyword match only | `"Title or abstract contains your search terms."` |

---

## The Suggestion System

**This is separate from search.** Suggestions appear as you type — they are powered by the **frontend's in-memory analytics data**, not by calling the search endpoint. This means zero network latency: suggestions are instant.

### Two modes

#### Before typing (cold-start recommendations)
Shows the **8 most-cited papers** in the database. These are the most visible pieces of research on SCOUP — giving users a sense of what's here before they commit to a query.

```
topPapers sorted by citations descending → first 8 titles shown
```

#### While typing (live autocomplete)
Searches across all titles and topics in the index. Ranking by match quality:

| Tier | Condition | Example (typing "bio") |
|---|---|---|
| 1 | Exact match | `"bio"` |
| 2 | Entry starts with query | `"biology"` |
| 3 | A word in entry starts with query | `"molecular biology"` |
| 4 | Entry contains query anywhere | `"antibiotic resistance"` |

Within each tier, higher-weight entries appear first.

### Suggestion index weights

| Source | Weight | Notes |
|---|---|---|
| Paper titles | **50** | Highest — what users most often search for |
| Faculty names | **35** | People search by name |
| Faculty research interests | **28** | Curated topics |
| Paper keywords | **30** | Specific terminology |
| Project titles | **25** | — |
| Patent titles | **22** | — |
| Faculty AI keywords | **22** | Academic Metrics categories |
| Department affiliations | **18** | Context |
| Project/patent keywords | **16–18** | — |

Entries are deduplicated case-insensitively. Same text from multiple sources keeps the highest weight.

Up to **8 suggestions** shown at a time.

---

## Response Format

```json
{
  "query": "machine learning healthcare",
  "count": 16,
  "results": [
    {
      "type": "faculty",
      "confidence": 78,
      "aiJustification": "Shuangquan Wang works in Machine Learning Challenges, Machine learning in sports science.",
      "matchedKeywords": ["machine", "learning"],
      "data": {
        "id": "...",
        "name": "Shuangquan Wang",
        "title": "Assistant Professor",
        "department": "Computer Science",
        "email": "...",
        "phone": "...",
        "photo": "...",
        "researchInterests": ["Machine Learning Challenges", "Machine learning in sports science"],
        "themes": ["..."],
        "metricsProfile": { "totalCitations": 12, "articleCount": 8, "averageCitations": 1.5 }
      }
    },
    {
      "type": "paper",
      "confidence": 85,
      "aiJustification": "Title directly matches your search terms.",
      "matchedKeywords": ["machine", "learning"],
      "data": {
        "id": "...",
        "title": "Machine Learning for Healthcare Diagnostics",
        "authors": ["Dr. Jane Smith", "..."],
        "year": 2023,
        "journal": "...",
        "abstract": "...",
        "doi": "...",
        "link": "https://doi.org/...",
        "semanticScore": 42.3,
        "citations": 47
      }
    }
  ]
}
```

Results are sorted by `confidence` descending. Faculty and papers are interleaved — a high-confidence paper outranks a weak faculty match.

---

## Configuration

| Setting | Value | Location |
|---|---|---|
| Embedding model | `text-embedding-3-small` | `academic/semantic.py` |
| Embedding dimensions | 1,536 | OpenAI model spec |
| Semantic threshold | `0.22` | `views.py → unified_search` |
| Exact title match minimum confidence | `85%` | `views.py` |
| Max faculty returned (pre-filter) | 80 | `views.py` |
| Max papers — semantic | unlimited (all above threshold) | `views.py` |
| Max papers — keyword stage 1 (AND) | unlimited | `views.py` |
| Max papers — keyword stage 2 (OR) | 20 | `views.py` |
| Max papers in final results | 15 | `views.py` |
| Faculty confidence threshold | 30% | `views.py` |
| Confidence ceiling | 95% | `views.py` |
| Max suggestions shown | 8 | `searchEngine.ts` |
| Cold-start suggestions | Top 8 by citation count | `searchEngine.ts` |
| IDF formula | `log(N / (1 + df)) + 1` | `views.py` |

---

## What Is Not Yet Implemented

| Feature | Notes |
|---|---|
| Patent search | Patents are in the database but not yet returned by unified_search |
| Project search | Same — projects exist but are not scored/returned yet |
| Faculty embeddings | Only papers have embeddings; faculty are keyword-scored only |
| Query expansion | No synonym expansion (e.g., "ML" → "machine learning") yet |
| AI-generated justifications | Justifications are template-generated, not LLM-written |
| Search feedback loop | No click/dwell tracking yet |
| Personalization | Suggestions are data-driven, not user-history-driven |

---

## Key Design Decisions

**Why not use a search library like Elasticsearch or Solr?**  
The dataset is small enough (~367 faculty, ~511 papers) that real-time scoring in Python is fast. Adding a search cluster would add operational complexity without meaningful speed gains at this scale.

**Why IDF instead of a fixed weight per word?**  
Fixed weights treat "biology" and "proteomics" equally. IDF makes "proteomics" (rare) a stronger signal than "biology" (common), which better reflects what a user means when they type a specific term.

**Why separate keyword trust levels instead of one text blob?**  
Early testing showed blob matching caused "biology" to match ~95 faculty because the word appears buried in Academic Metrics taxonomy strings for almost everyone. Trust levels restrict scoring to specific, curated keywords first.

**Why 0.22 semantic threshold instead of the standard 0.35?**  
Standard NLP benchmarks assume long, full-sentence queries. A single-word search like "nursing" or "biology" produces naturally lower cosine similarity scores (~0.25–0.32 against the best papers) because the query vector is low-information. Raising the threshold to 0.35 would silently return zero paper results for short queries. The 0.22 floor catches legitimate matches while the confidence scaling (40% floor) makes it clear these are weaker signals.

**Why do both semantic and keyword passes always run?**  
Early versions used keyword search only as a fallback when semantic found nothing. This caused a critical bug: if semantic search returned 15 results, the keyword fallback was skipped entirely. A paper with an exact title match but a low semantic similarity score (e.g., a paper with no stored embedding) would never appear. Running both passes and merging with MAX() ensures exact title matches always surface at 85%+ regardless of semantic score.

**Why show most-cited papers as cold-start suggestions?**  
Before a user types anything, showing the most-cited papers gives them an honest picture of what's in SCOUP — the research that has had the most academic impact. It is more informative than generic topic keywords and more concrete than faculty names.

**Why are suggestions built from analytics data, not from a backend call?**  
The analytics data is already downloaded when the home page loads. Building the suggestion index from it means zero additional API calls — instant suggestions with no latency. The tradeoff is that suggestions reflect what's in the dataset, not what users have historically searched for.
