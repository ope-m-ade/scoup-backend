# Semantic Search Notes (Hold for Later)

Status: Draft notes only. Not part of active implementation workflow yet.

## Scope

### External Portal

-   Single search bar with 3 modes:
    -   Papers
    -   Projects
    -   Faculty Keywords
-   Result card fields:
    -   title
    -   short description
    -   faculty affiliations
    -   confidence percentage
-   Contact pathways for external stakeholders to reach faculty.

### Middle Layer (Intelligent Search Engine)

-   Query understanding:
    -   infer intent (paper vs project vs faculty)
    -   query expansion via synonyms/related terms
-   Hybrid matching:
    -   keyword match
    -   semantic (meaning-based) match
-   Ranking and calibration:
    -   convert raw relevance scores to confidence percentages
    -   display confidence bands:
        -   Very Likely: 90-100%
        -   Likely: 70-89%
        -   Possibly Related: \<70%
-   Explainability and feedback:
    -   one-line rationale per result
    -   collect signals (thumbs up/down, clicks, dwell time)

## Proposed Architecture

1.  Data/index layer

-   Build a normalized search document per entity:
    -   `type` (`paper|project|faculty`)
    -   `title`, `summary`, `content`
    -   `keywords`, `department`, `faculty_ids`
-   Store:
    -   lexical index (BM25/trigram)
    -   vector embeddings (semantic)

2.  Query pipeline

-   Normalize query text
-   Expand with domain synonyms/acronyms
-   Route to selected mode (or infer mode if not explicit)

3.  Retrieval

-   Run lexical retrieval and vector retrieval in parallel
-   Merge candidate set and rerank

4.  Ranking and confidence

-   Combine weighted features:
    -   semantic similarity
    -   lexical score
    -   field matches (title/keywords/department)
-   Calibrate to confidence % using judged examples

5.  Output format

-   Return:
    -   ranked results
    -   confidence % + band
    -   concise rationale
    -   contact pathway metadata

6.  Learning loop

-   Log interactions:
    -   impressions
    -   clicks
    -   dwell
    -   explicit feedback
-   Periodically update:
    -   weights
    -   synonym sets
    -   confidence calibration model

## Suggested API Shape (Future)

### Search

`POST /api/search`

Request:

``` json
{
  "query": "machine learning for healthcare",
  "mode": "papers",
  "top_k": 20
}
```

Response item (example):

``` json
{
  "id": "paper_123",
  "type": "paper",
  "title": "...",
  "description": "...",
  "faculty_affiliations": ["Dr. A", "Dr. B"],
  "confidence": 87,
  "band": "Likely",
  "rationale": "Semantic alignment with abstract; keyword match in title.",
  "contact": {
    "faculty_id": 42,
    "email": "..."
  }
}
```

### Feedback

`POST /api/search/feedback`

Request:

``` json
{
  "query_id": "q_abc123",
  "result_id": "paper_123",
  "event": "thumbs_up"
}
```

## Rollout Plan (When resumed)

1.  Build search document/index table and indexer.
2.  Implement hybrid retrieval endpoint.
3.  Add confidence banding and rationale generation.
4.  Add feedback collection endpoint.
5.  Calibrate confidence with collected relevance data.

## Non-Goals for now

-   Not wiring into frontend workflow yet.
-   Not replacing current suggestion pipeline immediately.

## Resume Tag

Use this tag later for quick grep: `SEMANTIC_SEARCH_NOTES_V1`