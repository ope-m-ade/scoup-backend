# SCOUP Presentation Notes

------------------------------------------------------------------------

## Search Engine

SCOUP's search engine operates on two simultaneous layers — lexical and semantic — that work together to return the most relevant results.

### Layer 1: Lexical Search

Lexical search is intelligent keyword matching. When a user enters a query, the engine:

1.  **Expands abbreviations** — e.g. "LLM" is automatically expanded to "large language models", "AI" to "artificial intelligence", "ML" to "machine learning", and so on. This is handled by a hardcoded query expansion dictionary.
2.  **Strips noise words** — common words like "the", "and", "for" are removed so only meaningful terms are matched.
3.  **Searches across multiple fields** — title, keywords, abstract, bio, department — each weighted differently.
4.  **Assigns a confidence score** based on *where* and *how well* the match was found:
    -   Exact title/name match → 99
    -   Phrase found in title → 97
    -   Match in AI or faculty keywords → 95
    -   Match in abstract or imported categories → 78
    -   Anything below 40 is discarded

This layer requires no API key and no external services. It runs entirely within the system and is a robust, reliable engine on its own.

------------------------------------------------------------------------

### Layer 2: Semantic Search

Semantic search understands *meaning*, not just keywords. It is powered by OpenAI embeddings.

**How it works:**

1.  A management command (`python manage.py embed_papers`) is run on the backend. This sends each paper's title and abstract to OpenAI, which converts the text into a vector — a list of \~1536 numbers that mathematically encode the paper's meaning.
2.  These vectors are stored in the database on each paper record.
3.  When a user submits a search query, that query is *also* converted into a vector in real time (via OpenAI API).
4.  The engine then calculates the **cosine similarity** between the query vector and every paper's stored vector. Cosine similarity measures how close two pieces of text are in meaning within that vector space — a score close to 1.0 means very similar, close to 0.0 means unrelated.
5.  Papers above a similarity threshold of 0.22 are surfaced as semantic matches, with confidence scores ranging from 40 to 85.

**The key distinction from lexical search:** semantic search finds papers based on meaning even when the exact words don't match.

> **Example:** The database contains a paper titled *"TremorSense: Tremor Detection for Parkinson's Disease Using Convolutional Neural Network."* Its keywords are all health and neuroscience terms — nothing about "wearable technology" or "disease classification."
>
> -   Searching **"tremor parkinson"** → lexical search finds it (words in title). ✅
> -   Searching **"wearable disease classification"** → lexical search finds nothing. Semantic search finds it because the paper's meaning is close to that query in vector space. ✅

------------------------------------------------------------------------

### How the Two Layers Blend

Both layers run simultaneously on every search. For each paper:

-   If found by **both** → the higher confidence score wins
-   If found by **lexical only** → lexical score is used (max 99)
-   If found by **semantic only** → semantic score is used (max 85)
-   If found by **neither** → not returned

Lexical scores are capped higher intentionally — an exact keyword match is trusted more than AI similarity.

------------------------------------------------------------------------

### Graceful Fallback

One of the strengths of this system is that it **does not depend on AI to function.** If the OpenAI API key is unavailable or the semantic layer is not running, the lexical engine continues to operate and return high-quality results. The system degrades gracefully rather than breaking.

This means SCOUP can serve users effectively even without an active API key, while benefiting from the additional intelligence of semantic search when it is available.

------------------------------------------------------------------------

### What the OpenAI Key Powers

| Feature                                     | Requires OpenAI Key |
|---------------------------------------------|---------------------|
| Lexical search                              | ❌ No               |
| "AI justification" text in results          | ❌ No (hardcoded)   |
| Semantic search (real-time query embedding) | ✅ Yes              |
| Paper embedding generation (`embed_papers`) | ✅ Yes              |
| CV upload → paper extraction                | ✅ Yes              |
| Generate faculty bio                        | ✅ Yes              |
| Generate research interests                 | ✅ Yes              |
| Generate paper keywords                     | ✅ Yes              |

------------------------------------------------------------------------

### "Did You Mean?" Spell Correction

When a search returns sparse or low-confidence results, the system automatically suggests a corrected query — similar to Google's "Did you mean?" feature.

**How it works:**
1. After search completes, if fewer than 3 results are returned OR all results score below 70% confidence, the correction check triggers
2. Each word in the query is compared against a word pool built from every paper title, faculty name, and keyword already in the database
3. Levenshtein distance (edit distance) measures how many character changes separate the typed word from known words — if a known word is within 2 edits, it's suggested as a correction
4. The corrected query is shown as a clickable link — clicking it re-runs the search automatically

**Example:** Typing "comporter" → system finds "computer" is 2 edits away → suggests "Did you mean **computer**?"

**Key property:** Fully automatic. The word pool is built from live database content — no manual list to maintain. As new faculty and papers are added, the correction vocabulary grows automatically.

---

### Things to Add / Future Improvements

-   [ ] Admin dashboard button to trigger embedding generation for new papers (so terminal access is not required after each import)
-   [ ] Auto-generate embeddings when faculty publish new papers
-   [ ] Auto-run `embed_papers` after Academic Metrics import pipeline