# SCOUP Backend Architecture and API

## Purpose

The backend is a Django REST API for faculty profiles, research outputs, search data, collaboration inquiries, admin workflows, and support tickets.

## Stack

- Django 5.2
- Django REST Framework
- Simple JWT
- PostgreSQL on Render
- SQLite for local development
- OpenAI API for optional AI-assisted features

## Project Structure

| Path | Purpose |
| --- | --- |
| `scoupdb/settings.py` | Django settings, database, CORS, static/media, email |
| `scoupdb/urls.py` | Root routes: `/admin/`, `/api/` |
| `academic/models.py` | Data models |
| `academic/serializers.py` | DRF serializers |
| `academic/urls.py` | API routes |
| `academic/views/` | Faculty, paper/project/patent, admin, contact, search views |
| `academic/inquiry_views.py` | Collaboration inquiry and admin message endpoints |
| `academic/ticket_views.py` | Public/faculty/admin support ticket endpoints |
| `academic/search_engine.py` | Main lexical search/ranking engine |
| `academic/semantic.py` | OpenAI embedding helpers |
| `academic/management/commands/` | Import, keyword, and embedding management commands |

## Core Models

### Faculty

Represents both imported faculty records and registered faculty users.

Important fields:

- `user`
- `faculty_id`
- `first_name`, `last_name`, `name`
- `email`, `institutional_email`, `institutional_email_verified`
- `title`, `bio`, `research_interests`
- `primary_school`, `primary_department`
- `schools`, `departments`
- `is_approved`, `profile_visibility`, `review_status`
- `article_count`, `total_citations`, `average_citations`

### Paper

Faculty-linked publication records.

Important fields:

- `doi`
- `title`
- `abstract`
- `journal`
- `date_published`
- `authors`
- `keywords`, `ai_keywords`, `faculty_keywords`
- `tc_count`
- `status`

Paper status values:

- `draft`
- `in-review`
- `published`

### Project

Faculty-linked project records.

Important fields:

- `title`
- `description`
- `start_date`, `end_date`
- `faculty`
- `funding_source`
- `status`
- `keywords`
- `link`
- `is_open_to_collaboration`
- `collaboration_invitation`
- `allow_student_interest`

### Patent

Faculty-linked patent records.

Important fields:

- `title`
- `abstract`
- `patent_number`
- `filing_date`, `issue_date`
- `faculty`
- `link`
- `aiKeywords`

### CollaborationInquiry

Stores external inquiries, faculty collaboration inquiries, project interest, and admin-to-faculty messages.

Important fields:

- `source_type`: `faculty`, `external`, `admin`
- `from_faculty`
- `requester_name`, `requester_email`, `requester_organization`
- `recipient_faculty`
- `sender_admin`
- `message_subject`
- `target_faculty_name`, `target_faculty_id`
- `target_project`, `target_project_title`
- `requester_role`
- `note`
- `status`: `pending`, `reviewed`, `closed`
- `admin_notes`

### SupportTicket

Stores support requests from public visitors or faculty users.

Ticket types:

- `bug`
- `account`
- `content`
- `feature`
- `other`

Statuses:

- `open`
- `in_progress`
- `resolved`
- `closed`

## API Summary

All paths below are prefixed with `/api`.

### Auth

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/token/` | Public | Login, returns access/refresh JWTs |
| POST | `/token/refresh/` | Public | Refresh access token |
| POST | `/auth/forgot-password/` | Public | Send reset email |
| POST | `/auth/reset-password/` | Public | Reset password |
| POST | `/auth/change-password/` | User | Change password |
| POST | `/auth/send-otp/` | User | Send institutional email OTP |
| POST | `/auth/verify-otp/` | User | Verify institutional email |

### Public Search

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/public/search-data/` | Public | Public faculty/papers/patents/projects dataset |
| GET | `/search/?q=<query>` | Public | Unified public search |
| GET | `/categories/` | Public | Browse categories |
| GET | `/categories/<slug>/` | Public | Category detail |

### Faculty Profile

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/faculty/signup/` | Public | Faculty signup |
| GET/PATCH | `/faculty/me/` | Faculty | Current faculty profile |
| GET | `/faculty/me/suggestions/` | Faculty | Profile merge suggestions |
| POST | `/faculty/me/suggestions/<id>/approve/` | Faculty | Accept suggested imported profile |
| POST | `/faculty/me/suggestions/<id>/reject/` | Faculty | Reject suggestion |
| POST | `/faculty/upload-photo/` | Faculty | Upload profile photo |

### Papers, Projects, Patents

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET/POST | `/papers/` | Faculty | List/create own papers |
| GET/PUT/PATCH/DELETE | `/papers/<id>/` | Faculty | Manage own paper |
| GET/POST | `/projects/` | Faculty | List/create own projects |
| GET/PUT/PATCH/DELETE | `/projects/<id>/` | Faculty | Manage own project |
| GET/POST | `/patents/` | Faculty | List/create own patents |
| GET/PUT/PATCH/DELETE | `/patents/<id>/` | Faculty | Manage own patent |

Project collaboration fields:

```json
{
  "is_open_to_collaboration": true,
  "collaboration_invitation": "Students and partners are welcome to reach out.",
  "allow_student_interest": true
}
```

### CV and AI Tools

These endpoints require faculty authentication. Most require `OPENAI_API_KEY`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/faculty/upload-cv-papers/` | Extract structured paper records from CV PDF |
| POST | `/faculty/confirm-cv-items/` | Save confirmed extracted CV items |
| GET | `/faculty/paper-search/?q=<title-or-doi>` | Search external paper metadata |
| POST | `/faculty/extract-abstract/` | AI abstract extraction |
| POST | `/faculty/generate-keywords/` | AI paper keyword generation |
| POST | `/faculty/generate-bio/` | AI faculty bio generation |
| POST | `/faculty/generate-research-interests/` | AI research-interest generation |
| POST | `/faculty/generate-profile-keywords/` | AI profile keyword generation |

### Collaboration and Messages

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/network/inquire/` | Public/faculty | Submit faculty inquiry or project interest |
| GET | `/faculty/inquiries/` | Faculty | Messages/inquiries aimed at current faculty |
| PATCH | `/faculty/inquiries/<id>/` | Faculty | Mark received inquiry reviewed/closed |
| POST | `/admin/faculty/<id>/message/` | Admin | Send direct admin message to faculty |
| GET | `/admin/inquiries/` | Admin | List all inquiries/messages |
| PATCH | `/admin/inquiries/<id>/` | Admin | Update status/admin notes |

Project interest payload example:

```json
{
  "target_project_id": 8,
  "target_project_title": "Community Water Quality Monitoring",
  "target_faculty_name": "Dr. Jane Smith",
  "requester_name": "Taylor Student",
  "requester_email": "taylor@example.com",
  "requester_role": "student",
  "note": "I am interested in helping with field data collection."
}
```

### Support Tickets

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/support/ticket/` | Public | Submit public support ticket |
| GET | `/faculty/tickets/` | Faculty | List own support tickets |
| POST | `/faculty/tickets/submit/` | Faculty | Submit faculty support ticket |
| GET | `/admin/tickets/` | Admin | List all tickets |
| PATCH | `/admin/tickets/<id>/` | Admin | Update ticket status/admin notes |

### Admin

| Method | Path | Purpose |
| --- | --- | --- |
| GET/PATCH | `/admin/me/` | Current admin user |
| GET | `/admin/faculty/` | Faculty management list |
| POST | `/admin/faculty/bulk-action/` | Bulk approve/reject/toggle visibility |
| PATCH/DELETE | `/admin/faculty/<id>/` | Update/delete faculty |
| POST | `/admin/faculty/<id>/approve/` | Approve faculty |
| POST | `/admin/faculty/<id>/reject/` | Reject faculty |
| GET | `/admin/stats/` | Platform stats |
| GET | `/admin/audit-log/` | Admin audit log |
| GET/POST | `/admin/contact/team/` | Contact team management |
| PATCH | `/admin/contact/settings/` | Contact page settings |

## Deployment Notes

- Render runs migrations during `build.sh`.
- Do not squash migrations during active handoff unless every deployed environment is known and coordinated.
- AI features require `OPENAI_API_KEY`; the rest of the platform degrades gracefully without it.
- Uploaded media needs persistent storage for long-term production use.
