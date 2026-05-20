# SCOUP Deployment Guide

## Current Hosting

Frontend: Vercel

``` text
TODO: add current live frontend URL
```

Backend: Render

``` text
TODO: add current backend API URL
```

Ownership note: the current live services may be connected to the original maintainer's personal Vercel/Render accounts. For school ownership, transfer the services or recreate them under school-controlled accounts.

## Backend Deployment

Render configuration is in `render.yaml`.

Build command:

``` bash
./build.sh
```

Start command:

``` bash
gunicorn scoupdb.wsgi:application --timeout 120 --workers 2
```

`build.sh` runs:

``` bash
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
```

## Backend Environment Variables

Required:

``` bash
SECRET_KEY=
DEBUG=False
DATABASE_URL=
```

Recommended:

``` bash
RENDER=True
OPENAI_API_KEY=
DEFAULT_FROM_EMAIL=
```

Email, if SMTP is enabled:

``` bash
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
```

## Frontend Deployment

Vercel should build with:

``` bash
npm install
npm run build
```

Frontend environment variable:

``` bash
VITE_API_BASE_URL=https://<backend-host>/api
```

## OpenAI Dependency

`OPENAI_API_KEY` is required for:

-   CV upload extraction
-   AI keyword generation
-   AI biography/research-interest generation
-   Semantic embedding generation

Without the key, these AI features are unavailable, but the rest of the application still works.

## Migration Checklist

Before pushing/deploying:

``` bash
python manage.py makemigrations --check
python manage.py migrate --plan
python manage.py check
npm run build
```

On Render, migrations run during build. If a migration fails with a duplicate-column error, inspect the production database before faking or editing migrations.

## Media Files

Uploaded faculty photos are stored under Django media storage. Render's normal filesystem may not be durable across deploys unless persistent storage is configured. For a long-term school deployment, use persistent disk storage or external object storage.