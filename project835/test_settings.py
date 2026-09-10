"""Fast SQLite settings used only by the repository test suite."""

from localsettings import *  # noqa: F401,F403

MIGRATION_MODULES = {
    "accounts": None,
    "admin_panel": None,
    "edi835": None,
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
ROOT_URLCONF = "project835.test_urls"
