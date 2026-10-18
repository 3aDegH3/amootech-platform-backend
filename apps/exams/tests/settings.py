import os

# Isolated test database; never use the developer's database or provider credentials.
for key, value in {
    "DJANGO_SECRET_KEY": "exam-unit-tests-only", "DJANGO_ALLOWED_HOSTS": "testserver,localhost",
    "POSTGRES_DB": "unused", "POSTGRES_USER": "unused", "POSTGRES_PASSWORD": "unused",
    "POSTGRES_HOST": "unused", "POSTGRES_PORT": "5432", "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
}.items():
    os.environ.setdefault(key, value)
from config.settings import *  # noqa: F403,E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
QUIZ24_API_KEY = ""
QUIZ24_USER_ID = ""
