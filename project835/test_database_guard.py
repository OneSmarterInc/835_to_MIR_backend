from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from project835.database_guard import require_postgresql


class PostgreSQLGuardTestCase(SimpleTestCase):
    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.postgresql"}})
    def test_postgresql_is_accepted(self):
        require_postgresql()

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3"}})
    @patch.dict("os.environ", {}, clear=True)
    def test_sqlite_is_rejected_by_default(self):
        with self.assertRaises(ImproperlyConfigured):
            require_postgresql()

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3"}})
    @patch.dict("os.environ", {"ALLOW_SQLITE_FOR_TESTS": "1"}, clear=True)
    def test_sqlite_requires_explicit_test_override(self):
        require_postgresql()
