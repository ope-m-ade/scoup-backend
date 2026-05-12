from pathlib import Path
import os
import dj_database_url
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if os.environ.get("RENDER"):
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
    SECRET_KEY = "dev-secret-key-not-for-production"

DEBUG = os.environ.get("DEBUG", "") != "False"

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "scoup-backend.onrender.com",
    os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""),
]
ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]  # remove empty strings


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "academic",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # must be first — before any middleware that generates responses
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
}

ROOT_URLCONF = 'scoupdb.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'scoupdb.wsgi.application'


DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        ssl_require=os.environ.get("RENDER", False),
    )
}

if os.environ.get("RENDER"):
    DEBUG = False
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

FRONTEND_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://localhost:3001",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "https://scoup-frontend.vercel.app",
    "https://scoup-frontend-gfuswkjyy-ope-m-ades-projects.vercel.app",
    "https://scoup-frontend-2-0.vercel.app",
    "https://scoup-frontend-2-0-2poqqdhc7-ope-m-ades-projects.vercel.app",
    "https://scoup-frontend-2-0-3v4op2cv2-ope-m-ades-projects.vercel.app",
]

CORS_ALLOWED_ORIGINS = FRONTEND_ORIGINS
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://scoup-frontend[\w-]*\.vercel\.app$",
    r"^https://scoup-frontend-2-0[\w-]*\.vercel\.app$",
]
CORS_ALLOW_CREDENTIALS = True  # needed because requests include Authorization header
CSRF_TRUSTED_ORIGINS = FRONTEND_ORIGINS + [
    "https://*.vercel.app",
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Email — configure via environment variables on Render
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST        = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT        = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_USE_TLS     = os.environ.get("EMAIL_USE_TLS", "True") == "True"
EMAIL_USE_SSL     = os.environ.get("EMAIL_USE_SSL", "False") == "True"
EMAIL_HOST_USER   = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@scoup.app")
