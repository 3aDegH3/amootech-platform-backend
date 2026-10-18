#!/bin/sh
set -eu

# Bound connection retries so a bad configuration exits instead of hanging forever.
python - <<'PY'
import os
import sys
import time
import django
from django.db import connections

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
connection = connections["default"]
connection.settings_dict.setdefault("OPTIONS", {})["connect_timeout"] = 3
for attempt in range(1, 11):
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        connection.close()
        print("Database connection verified.", flush=True)
        break
    except Exception:
        connection.close()
        if attempt == 10:
            print("Database connection failed. Check POSTGRES_* settings and the existing database role password. Changing .env does not change passwords in an initialized volume.", file=sys.stderr)
            sys.exit(1)
        print(f"Waiting for database ({attempt}/10)...", flush=True)
        time.sleep(2)
PY

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
