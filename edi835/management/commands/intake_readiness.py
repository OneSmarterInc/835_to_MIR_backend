"""Read-only runtime checks; gateway enforcement still needs an HTTP probe."""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Check production intake security configuration without exposing secrets."

    def handle(self, *args, **options):
        checks = {
            "postgresql": connection.vendor == "postgresql",
            "debug_disabled": not settings.DEBUG,
            "mfa_enforced": bool(settings.MFA_ENFORCEMENT_ENABLED),
            "csrf_middleware": "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE,
            "session_secure": bool(settings.SESSION_COOKIE_SECURE),
            "csrf_secure": bool(settings.CSRF_COOKIE_SECURE),
            "csrf_https_origins": bool(settings.CSRF_TRUSTED_ORIGINS) and all(
                origin.startswith("https://") for origin in settings.CSRF_TRUSTED_ORIGINS),
            "bounded_uploads": 0 < settings.INTAKE_MAX_BYTES <= settings.INTAKE_MAX_REQUEST_BYTES
                and settings.DATA_UPLOAD_MAX_MEMORY_SIZE == settings.INTAKE_MAX_REQUEST_BYTES
                and 0 < settings.INTAKE_MAX_FILES <= 1000,
            "utc_timestamps": bool(settings.USE_TZ),
        }
        self.stdout.write(json.dumps({"checks": checks, "gateway_http_probe_required": True}))
        if not all(checks.values()):
            raise CommandError("Production intake checks failed: " + ", ".join(key for key, ok in checks.items() if not ok))
