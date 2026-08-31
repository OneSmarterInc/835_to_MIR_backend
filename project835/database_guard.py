"""Fail fast when the application is not configured for PostgreSQL."""

import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


POSTGRES_ENGINES = {
    "django.db.backends.postgresql",
    "django.contrib.gis.db.backends.postgis",
}


def require_postgresql():
    engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    if engine in POSTGRES_ENGINES:
        return
    if os.getenv("ALLOW_SQLITE_FOR_TESTS") == "1" and engine == "django.db.backends.sqlite3":
        return
    raise ImproperlyConfigured(
        "PostgreSQL is required. Set DATABASE_URL for the MIR production database; "
        "SQLite fallback is disabled."
    )
