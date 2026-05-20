# CV Upload & Paper Extraction

Faculty members can upload a PDF of their CV or resume to automatically populate their research profile with papers, patents, and projects. Nothing is saved until the faculty reviews and confirms each item.

---

## How It Works

```
PDF Upload → Text Extraction → OpenAI Parsing → CrossRef Enrichment → Faculty Review → Confirm & Save
```

### Step 1 — Upload the PDF

**Endpoint:** `POST /api/faculty/upload-cv-papers/`  
**Auth:** Faculty JWT required  
**Body:** `multipart/form-data` with a `file` field containing the PDF

Constraints:
- File must be a PDF (`application/pdf`)
- Maximum size: 10 MB
- PDF must contain readable text (scanned/image-only PDFs are rejected)

---

### Step 2 — Text Extraction

The server uses `pdfplumber` to extract raw text from all pages of the uploaded PDF.

---

### Step 3 — OpenAI Parsing

The extracted text is sent to OpenAI (requires `OPENAI_API_KEY` in environment). The model returns a structured JSON object with four sections:

```json
{
  "profile": {
    "title": "Associate Professor",
    "bio": "Dr. Smith researches...",
    "department": "Computer Science"
  },
  "papers": [
    {
      "title": "Deep Learning for X",
      "year": 2022,
      "journal": "Journal of AI Research",
      "doi": "10.1234/example",
      "authors": "Smith, J., Lee, K."
    }
  ],
  "patents": [
    {
      "title": "Method for Y",
      "patent_number": "US12345678",
      "year": 2021,
      "inventors": "Smith, J."
    }
  ],
  "projects": [
    {
      "title": "Community Water Quality Study",
      "description": "NSF-funded research into...",
      "year": 2023
    }
  ]
}
```

---

### Step 4 — CrossRef / Semantic Scholar Enrichment

For each extracted paper, the backend attempts to fetch a real abstract from external sources:

- **CrossRef** — looked up by DOI (if present) or title
- **Semantic Scholar** — fallback if CrossRef returns no abstract

Enriched abstracts replace the AI-guessed placeholder before the data is returned to the frontend.

---

### Step 5 — Faculty Review

The enriched extraction result is returned to the frontend **without saving anything yet**. The faculty sees a review screen where they can:

- Accept or reject individual papers, patents, and projects
- Edit titles, years, journals, or abstracts inline
- Trigger AI keyword generation on any paper before confirming

---

### Step 6 — Confirm & Save

**Endpoint:** `POST /api/faculty/confirm-cv-items/`  
**Auth:** Faculty JWT required  
**Body:**

```json
{
  "profile": { "title": "...", "bio": "...", "department": "..." },
  "papers":  [ { "title": "...", "year": 2022, "journal": "...", "doi": "...", "abstract": "...", "authors": "..." } ],
  "patents": [ { "title": "...", "patent_number": "...", "year": 2021, "inventors": "..." } ],
  "projects": [ { "title": "...", "description": "...", "year": 2023 } ]
}
```

Only the items the faculty approved are sent in the body. The backend:

- Matches papers by DOI (if present) or title to avoid duplicates
- Creates a `cv-import-<uuid>` placeholder DOI for papers with no DOI
- Only fills `title` and `bio` on the faculty profile if those fields are currently empty
- Returns a summary: `{ "papers": 3, "patents": 1, "projects": 2, "profile_updated": true }`

---

## Supporting Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/faculty/upload-cv-papers/` | Upload PDF, returns extracted + enriched items for review |
| `POST` | `/faculty/confirm-cv-items/` | Save the confirmed subset of extracted items |
| `GET`  | `/faculty/paper-search/?q=<title-or-doi>` | Search CrossRef / Semantic Scholar for a paper manually |
| `POST` | `/faculty/extract-abstract/` | Extract abstract from a single PDF file |
| `POST` | `/faculty/generate-keywords/` | AI keyword generation for a paper |
| `POST` | `/faculty/generate-bio/` | AI bio generation for a faculty profile |
| `POST` | `/faculty/generate-research-interests/` | AI research interest suggestions |
| `POST` | `/faculty/generate-profile-keywords/` | AI keyword suggestions for a faculty profile |

---

## Environment Requirements

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Required for all AI extraction and generation endpoints |

If `OPENAI_API_KEY` is not set, the upload endpoint returns `500: OpenAI API key not configured`.

---

## Error Responses

| Status | Cause |
|--------|-------|
| `400` | No file uploaded, not a PDF, file over 10 MB, or PDF has no readable text |
| `404` | Faculty profile not found for the authenticated user |
| `500` | OpenAI API key missing |
