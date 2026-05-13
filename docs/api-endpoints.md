# SCOUP API Endpoints

Base path: `/api`

Authentication: protected endpoints use JWT bearer auth.

``` http
Authorization: Bearer <access_token>
```

## Environment Notes

`OPENAI_API_KEY` is required for AI-assisted features:

-   CV upload paper extraction
-   PDF abstract extraction
-   AI keyword generation
-   AI faculty bio generation
-   AI research-interest generation
-   semantic embedding generation/search

If `OPENAI_API_KEY` is not configured, non-AI features still work: login, faculty profiles, public search data, lexical search, papers, projects, patents, contact settings, and collaboration inquiries.

CV upload abstract enrichment is intentionally disabled by default to avoid production worker timeouts.

Optional backend settings:

-   `CV_UPLOAD_ENRICH_ABSTRACTS=True`
-   `CV_UPLOAD_MAX_ABSTRACT_ENRICH=3`

## Auth

### Login

``` http
POST /api/token/
```

Body:

``` json
{
  "username": "faculty@example.edu",
  "password": "password"
}
```

Response:

``` json
{
  "access": "jwt",
  "refresh": "jwt"
}
```

### Refresh Token

``` http
POST /api/token/refresh/
```

Body:

``` json
{
  "refresh": "jwt"
}
```

## Public Search

### Public Search Dataset

``` http
GET /api/public/search-data/
```

Auth: public

Returns the public dataset used by the frontend search experience:

-   `facultyData`
-   `papersData`
-   `patentsData`
-   `projectsData`

Project records include collaboration fields:

``` json
{
  "isOpenToCollaboration": true,
  "collaborationInvitation": "We are looking for students or community partners...",
  "allowStudentInterest": true
}
```

### Unified Search

``` http
GET /api/search/?q=<query>
```

Auth: public

Runs the unified search engine across faculty, papers, patents, and projects.

### Categories

``` http
GET /api/categories/
GET /api/categories/<slug>/
```

Auth: public

Returns browse categories and category-specific faculty/research results.

## Faculty Profile

### Faculty Signup

``` http
POST /api/faculty/signup/
```

Auth: public

Creates a faculty login/profile request.

### Current Faculty Profile

``` http
GET /api/faculty/me/
PATCH /api/faculty/me/
```

Auth: faculty

Returns or updates the logged-in faculty member's profile.

### Upload Faculty Photo

``` http
POST /api/faculty/upload-photo/
```

Auth: faculty

Body: `multipart/form-data` with `photo`.

## Papers

### List/Create Papers

``` http
GET /api/papers/
POST /api/papers/
```

Auth: faculty

Lists or creates papers owned by the logged-in faculty member.

### Paper Detail

``` http
GET /api/papers/<id>/
PUT /api/papers/<id>/
PATCH /api/papers/<id>/
DELETE /api/papers/<id>/
```

Auth: faculty

### Bulk Publish Draft Papers

``` http
POST /api/faculty/papers/bulk-publish/
```

Auth: faculty

Body:

``` json
{
  "ids": [1, 2, 3]
}
```

or:

``` json
{
  "all_draft": true
}
```

### CV Upload Paper Extraction

``` http
POST /api/faculty/upload-cv-papers/
```

Auth: faculty

Requires: `OPENAI_API_KEY`

Body: `multipart/form-data` with `file` PDF.

Returns extracted papers for review. Nothing is saved until confirmation.

### Confirm CV Items

``` http
POST /api/faculty/confirm-cv-items/
```

Auth: faculty

Saves selected papers/patents/projects extracted from a CV.

### External Paper Search

``` http
GET /api/faculty/paper-search/?q=<title-or-doi>
```

Auth: faculty

Searches external paper metadata sources.

## AI Faculty Tools

These endpoints require `OPENAI_API_KEY`.

``` http
POST /api/faculty/extract-abstract/
POST /api/faculty/generate-keywords/
POST /api/faculty/generate-bio/
POST /api/faculty/generate-research-interests/
POST /api/faculty/generate-profile-keywords/
```

Auth: faculty

## Projects

### List/Create Projects

``` http
GET /api/projects/
POST /api/projects/
```

Auth: faculty

Create body:

``` json
{
  "title": "Community Water Quality Monitoring",
  "description": "A project studying local watershed health.",
  "start_date": "2026-01-01",
  "end_date": "",
  "status": "active",
  "funding_source": "University Grant",
  "keywords": ["water quality", "community science"],
  "is_open_to_collaboration": true,
  "collaboration_invitation": "Students and community partners are welcome to express interest.",
  "allow_student_interest": true
}
```

### Project Detail

``` http
GET /api/projects/<id>/
PUT /api/projects/<id>/
PATCH /api/projects/<id>/
DELETE /api/projects/<id>/
```

Auth: faculty

Only projects linked to the logged-in faculty member are accessible.

## Project Collaboration Interest and Faculty Inquiries

### Submit Collaboration Inquiry

``` http
POST /api/network/inquire/
```

Auth: public or faculty

Use this endpoint for both:

-   direct inquiries to visible faculty profiles
-   interest in projects marked `is_open_to_collaboration=true`

Anonymous request body:

``` json
{
  "target_faculty_name": "Dr. Jane Smith",
  "target_faculty_id": "12",
  "target_department": "Computer Science",
  "target_school": "School of Science",
  "requester_name": "Alex Johnson",
  "requester_email": "alex@example.com",
  "requester_organization": "Community Partner Org",
  "requester_role": "external collaborator",
  "note": "I would like to discuss a possible collaboration."
}
```

Project interest body:

``` json
{
  "target_faculty_name": "Dr. Jane Smith",
  "target_project_id": 8,
  "target_project_title": "Community Water Quality Monitoring",
  "target_department": "Biology",
  "requester_name": "Taylor Student",
  "requester_email": "taylor@example.com",
  "requester_role": "student",
  "note": "I am interested in helping with field data collection."
}
```

Notes:

-   Anonymous users must provide `requester_name` and `requester_email`.
-   Anonymous submissions are rate-limited to 5 per IP per hour.
-   If `target_project_id` is provided, the project must be open to collaboration.
-   Authenticated faculty submissions are linked to `from_faculty`.

### Faculty Inquiry Inbox

``` http
GET /api/faculty/inquiries/
```

Auth: faculty

Returns inquiries aimed at the logged-in faculty member or their projects.

### Update Faculty Inquiry Status

``` http
PATCH /api/faculty/inquiries/<id>/
```

Auth: faculty

Body:

``` json
{
  "status": "reviewed"
}
```

Allowed faculty statuses:

-   `reviewed`
-   `closed`

## Patents

### List/Create Patents

``` http
GET /api/patents/
POST /api/patents/
```

Auth: faculty

### Patent Detail

``` http
GET /api/patents/<id>/
PUT /api/patents/<id>/
PATCH /api/patents/<id>/
DELETE /api/patents/<id>/
```

Auth: faculty

## Network Discovery

``` http
GET /api/network/discovery/?q=<query>&limit=50
```

Auth: faculty

Returns collaboration recommendations for the logged-in faculty member.

## Contact Page

``` http
GET /api/contact/team/
GET /api/contact/settings/
```

Auth: public

## Admin

Admin endpoints require a JWT for a user with `is_staff=True`.

### Admin Current User

``` http
GET /api/admin/me/
PATCH /api/admin/me/
```

Auth: admin

### Admin Faculty Management

``` http
GET /api/admin/faculty/
GET /api/admin/faculty/?pending=true
PATCH /api/admin/faculty/<id>/
DELETE /api/admin/faculty/<id>/
POST /api/admin/faculty/<id>/approve/
POST /api/admin/faculty/<id>/reject/
POST /api/admin/faculty/bulk-action/
```

Auth: admin

### Admin Inquiries

``` http
GET /api/admin/inquiries/
GET /api/admin/inquiries/?source=faculty
GET /api/admin/inquiries/?source=external
PATCH /api/admin/inquiries/<id>/
```

Auth: admin

Admin inquiry records include project targeting fields when applicable:

``` json
{
  "target_project_id": 8,
  "target_project_title": "Community Water Quality Monitoring",
  "requester_role": "student"
}
```

Patch body:

``` json
{
  "status": "reviewed",
  "admin_notes": "Forwarded to project lead."
}
```

### Admin Stats and Audit Log

``` http
GET /api/admin/stats/
GET /api/admin/audit-log/
```

Auth: admin

### Admin Content Delete

``` http
GET /api/admin/papers/
DELETE /api/admin/papers/<id>/
DELETE /api/admin/projects/<id>/
DELETE /api/admin/patents/<id>/
```

Auth: admin

### Admin Contact Page

``` http
GET /api/admin/contact/team/
POST /api/admin/contact/team/
PATCH /api/admin/contact/team/<id>/
DELETE /api/admin/contact/team/<id>/
POST /api/admin/contact/team/<id>/photo/
PATCH /api/admin/contact/settings/
```

Auth: admin

## Deployment Checklist

Before deploying backend changes:

``` bash
python manage.py makemigrations --check
python manage.py migrate --plan
python manage.py check
```

For the project collaboration feature, migration `academic.0010_project_collaboration_inquiries` must be applied.