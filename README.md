# SCOUP Backend

This repo contains the Django REST backend for SCOUP.

## Tech Stack

- Django 5.2
- Django REST Framework
- Simple JWT
- PostgreSQL on Render
- SQLite for local development
- OpenAI API for AI-assisted features

## Local Setup

Create an environment file:

```bash
cp .env.example .env
```

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run migrations:

```bash
python manage.py migrate
```

Start the server:

```bash
python manage.py runserver
```

## Required Environment Variables

At minimum:

```bash
SECRET_KEY=replace-me
DEBUG=False
DATABASE_URL=postgres://...
```

Optional but needed for AI features:

```bash
OPENAI_API_KEY=...
```

Optional email settings:

```bash
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
DEFAULT_FROM_EMAIL=
```

## Deployment

Render uses:

```bash
./build.sh
```

The build script installs dependencies, collects static files, and runs migrations.

## Documentation

- [Backend architecture and API](docs/backend-architecture-and-api.md)
- [Deployment guide](docs/deployment.md)
- [Admin guide](docs/admin-guide.md)
- [Search engine](docs/search-engine.md)
