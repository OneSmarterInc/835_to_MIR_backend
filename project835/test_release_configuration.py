"""Release compatibility checks against isolated settings and real migrations."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parent.parent


class ReleaseConfigurationTests(SimpleTestCase):
    def run_python(self, source, extra_env=None):
        environment = os.environ.copy()
        environment.update({
            "SECRET_KEY": "isolated-release-test-key",
            "DATABASE_URL": "sqlite:///:memory:",
            "ALLOW_SQLITE_FOR_TESTS": "1", "DEBUG": "True",
            "DJANGO_SETTINGS_MODULE": "project835.settings",
        })
        environment.update(extra_env or {})
        result = subprocess.run(
            [sys.executable, "-c", source], cwd=ROOT, env=environment,
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dotenv_bootstraps_settings_without_overwriting_service_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "localsettings.py", root / "localsettings.py")
            (root / ".env").write_text(
                "SECRET_KEY=dotenv-test-key\nDATABASE_URL=postgresql://test:test@localhost/test\n"
                "DEBUG=False\nCORS_ALLOWED_ORIGINS=https://filex.onesmarter.com\n"
                "CSRF_TRUSTED_ORIGINS=https://filex.onesmarter.com\n", encoding="utf-8",
            )
            self.run_python('''
import os, runpy
from pathlib import Path
for key in ('SECRET_KEY', 'DATABASE_URL', 'DEBUG', 'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS'):
    os.environ.pop(key, None)
root = Path(os.environ['RELEASE_SETTINGS_ROOT'])
settings = runpy.run_path(str(root / 'localsettings.py'))
assert settings['SECRET_KEY'] == 'dotenv-test-key'
assert settings['DATABASES']['default']['ENGINE'] == 'django.db.backends.postgresql'
assert settings['BASE_DIR'] == root
assert settings['DEBUG'] is False
os.environ['SECRET_KEY'] = 'service-environment-wins'
settings = runpy.run_path(str(root / 'localsettings.py'))
assert settings['SECRET_KEY'] == 'service-environment-wins'
''', {"RELEASE_SETTINGS_ROOT": str(root)})

    def test_migration_compatibility_for_fresh_pre_revert_and_post_revert_databases(self):
        for history in ("fresh", "pre-revert", "post-revert"):
            with self.subTest(history=history):
                self.run_python('''
import os, django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
history = os.environ['RELEASE_HISTORY']
def migrate(name):
    call_command('migrate', 'accounts', name, verbosity=0)
if history == 'pre-revert':
    migrate('0023_merge_20260921_1832')
    apps = MigrationExecutor(connection).loader.project_state([
        ('accounts', '0023_merge_20260921_1832')]).apps
    user = apps.get_model('accounts', 'User').objects.create(
        email='migration@example.com', name='Synthetic', mobile='+15550005555', password='unusable')
    apps.get_model('accounts', 'WebAuthnCredential').objects.create(
        user=user, credential_id='synthetic-credential', public_key='synthetic-key')
if history != 'fresh':
    migrate('0025_demo_role')
migrate('0026_retire_webauthn_state')
# A second deployment must be a no-op, including preserved retired tables.
migrate('0026_retire_webauthn_state')
executor = MigrationExecutor(connection)
executor.loader.check_consistent_history(connection)
state = executor.loader.project_state([('accounts', '0026_retire_webauthn_state')])
assert ('accounts', 'webauthncredential') not in state.models
assert 'edi837_filename_format' not in state.models['accounts', 'client'].fields
with connection.cursor() as cursor:
    cursor.execute('SELECT COUNT(*) FROM webauthn_credential')
    assert cursor.fetchone()[0] == (1 if history == 'pre-revert' else 0)
    columns = connection.introspection.get_table_description(cursor, 'client')
    assert sum(column.name == 'edi837_filename_format' for column in columns) == 1
''', {"RELEASE_HISTORY": history})
