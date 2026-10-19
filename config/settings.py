import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ImproperlyConfigured(f"Required environment variable {name} is not set")
    return value


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [
    host.strip() for host in required_env("DJANGO_ALLOWED_HOSTS").split(",") if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.accounts",
    "apps.academics",
    "apps.planning",
    "apps.exams",
    "apps.daily_reports",
    "apps.core",
    "apps.telegram",
    "apps.admin_dashboard",
]

TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()
TELEGRAM_BOT_SERVICE_TOKEN = os.getenv("TELEGRAM_BOT_SERVICE_TOKEN", "").strip()
TELEGRAM_BOT_CONTROL_BASE_URL = os.getenv("TELEGRAM_BOT_CONTROL_BASE_URL", "").strip().rstrip("/")
TELEGRAM_BOT_CONTROL_TOKEN = os.getenv("TELEGRAM_BOT_CONTROL_TOKEN", "").strip()
QUIZ24_BASE_URL = os.getenv("QUIZ24_BASE_URL", "https://www.quiz24.ir/api/v1/").strip()
QUIZ24_API_KEY = os.getenv("QUIZ24_API_KEY", "").strip()
QUIZ24_USER_ID = os.getenv("QUIZ24_USER_ID", "").strip()
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000" if DEBUG else "").strip().rstrip("/")
QUIZ24_CALLBACK_URL = os.getenv("QUIZ24_CALLBACK_URL", "").strip()
QUIZ24_STUDENT_CALLBACK_URL = os.getenv("QUIZ24_STUDENT_CALLBACK_URL", "").strip()

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required_env("POSTGRES_DB"),
        "USER": required_env("POSTGRES_USER"),
        "PASSWORD": required_env("POSTGRES_PASSWORD"),
        "HOST": required_env("POSTGRES_HOST"),
        "PORT": required_env("POSTGRES_PORT"),
    }
}
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
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in required_env("CORS_ALLOWED_ORIGINS").split(",")
    if origin.strip()
]
CORS_EXPOSE_HEADERS = ["Content-Disposition", "Content-Length"]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Amootech Platform API",
    "DESCRIPTION": "Amootech educational counseling platform API.",
    "VERSION": "1.0.0",
}

# A production SMS backend must implement send(mobile, code, *, student_id, nonce).
STUDENT_OTP_BACKEND = os.getenv("STUDENT_OTP_BACKEND", "").strip()
STUDENT_OTP_DEV_ENABLED = os.getenv("STUDENT_OTP_DEV_ENABLED", "false").lower() in {"1", "true", "yes"}
STUDENT_OTP_DEV_OUTBOX = os.getenv("STUDENT_OTP_DEV_OUTBOX", "/tmp/amootech-mobile-otp")

# Explicit opt-in; local trash/archive never calls provider deletion.
QUIZ24_REMOTE_DELETE_ENABLED = os.getenv("QUIZ24_REMOTE_DELETE_ENABLED", "false").lower() == "true"
