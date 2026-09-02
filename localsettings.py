import os
import sys
import dj_database_url
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# SECURITY
# ============================================================

DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "t")

SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("Set a secure SECRET_KEY environment variable.")

ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS",
        "127.0.0.1,localhost,mir.onesmarter.com,50.17.152.89,api.onesmarter.com").split(",") if host.strip()]

CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000,https://835-to-mir-frontend-88miukawd-1smarterincs-projects.vercel.app,https://mir.onesmarter.com,https://835-to-mir-frontend.vercel.app").split(",") if origin.strip()]

SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
# ============================================================
# SMTP FIELD ENCRYPTION
# Used to encrypt SMTP passwords stored in the database.
# IMPORTANT: Keep this key secret. In production, load from
# an environment variable instead of hardcoding here.
# Rotate this key only if you re-encrypt all existing rows.
# ============================================================
SMTP_FIELD_ENCRYPTION_KEY = os.getenv("SMTP_FIELD_ENCRYPTION_KEY", "").strip()
SFTP_FIELD_ENCRYPTION_KEY = os.getenv("SFTP_FIELD_ENCRYPTION_KEY", "").strip()
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", "1800"))
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
MFA_ENFORCEMENT_ENABLED = "test" not in sys.argv

# Interim MPL reconciliation policy. Keep each disputed V7/V5 interpretation
# explicit until MPL confirms the authoritative waterfall.
MPL_RECON_MIR907_SOURCE = os.getenv("MPL_RECON_MIR907_SOURCE", "computed").strip().lower()
MPL_RECON_MIR908_SOURCE = os.getenv("MPL_RECON_MIR908_SOURCE", "computed").strip().lower()
MPL_RECON_INCLUDE_MPL920 = os.getenv("MPL_RECON_INCLUDE_MPL920", "true").lower() in ("true", "1", "yes")
MPL_RECON_WATERFALL_STEPS = tuple(
    value.strip().upper()
    for value in os.getenv("MPL_RECON_WATERFALL_STEPS", "MIR901,MIR907,MIR908,MPL920").split(",")
    if value.strip()
)


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",

    "accounts",
    "home",
    "converter",
    "edi835",
    "admin_panel",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "project835.drf_auth.ExistingSessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "EXCEPTION_HANDLER": "project835.drf_compat.compatible_exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
  
  "django.middleware.security.SecurityMiddleware",
        "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "project835.middleware.AdminAccessMiddleware",
    "project835.middleware.ClientAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ============================================================
# URL CONFIGURATION
# ============================================================

ROOT_URLCONF = "project835.urls"


# ============================================================
# TEMPLATES
# ============================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ============================================================
# WSGI / ASGI
# ============================================================

WSGI_APPLICATION = "project835.wsgi.application"
ASGI_APPLICATION = "project835.asgi.application"


# ============================================================
# DATABASE CONFIGURATION
# PostgreSQL is mandatory. Never silently create or use a SQLite database when
# DATABASE_URL is missing, because that can make a healthy service appear to
# have lost all production data.
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise ImproperlyConfigured(
        "DATABASE_URL is required. This application must use PostgreSQL."
    )

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    )
}

if DATABASES["default"]["ENGINE"] not in {
    "django.db.backends.postgresql",
    "django.contrib.gis.db.backends.postgis",
} and not (
    os.getenv("ALLOW_SQLITE_FOR_TESTS") == "1"
    and DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
):
    raise ImproperlyConfigured(
        "DATABASE_URL must use PostgreSQL; SQLite and other engines are disabled."
    )


# ============================================================
# CUSTOM USER MODEL
# ============================================================

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]


# ============================================================
# PASSWORD VALIDATION
# ============================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# ============================================================
# LANGUAGE / TIMEZONE
# ============================================================

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True


# ============================================================
# STATIC FILES
# ============================================================

STATIC_URL = "static/"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}


# ============================================================
# DEFAULT PRIMARY KEY
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ============================================================
# LOGIN / LOGOUT REDIRECTS
# ============================================================

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/home/"
LOGOUT_REDIRECT_URL = "/accounts/login/"


# ============================================================
# SESSION & CORS SECURITY
# ============================================================

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG  # False in local dev (HTTP), True in production (HTTPS)
CSRF_COOKIE_SECURE = not DEBUG     # False in local dev (HTTP), True in production (HTTPS)

# For cross-origin requests between Vercel and AWS, cookie SameSite must be "None" if Secure is enabled
# In local dev over HTTP, SameSite=None requires Secure=True which we don't set, so fall back to "Lax"
SESSION_COOKIE_SAMESITE = "None" if not DEBUG else "Lax"
CSRF_COOKIE_SAMESITE = "None" if not DEBUG else "Lax"

CORS_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,https://835-to-mir-frontend-88miukawd-1smarterincs-projects.vercel.app,https://835-to-mir-frontend.vercel.app").split(",") if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True
from corsheaders.defaults import default_headers

CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-file-name",
    "x-admin-screen",
]
# Django CSRF Trusted Origins for CORS POST requests


# ============================================================
# MEDIA FILES (FTP / LOCAL STORAGE COMPATIBLE)
# ============================================================

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# ============================================================
# SAMPLE DOCUMENTS CONFIGURATION
# ============================================================

SAMPLE_DOCUMENTS_DIR = BASE_DIR / "sample_docs"


# ============================================================
# LOGGING CONFIGURATION
# ============================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "project835.log" if (BASE_DIR / "logs").exists() else BASE_DIR / "project835.log",
            "maxBytes": 1024 * 1024 * 5,  # 5MB
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": True,
        },
        "accounts": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "admin_panel": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "edi835": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "converter": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
