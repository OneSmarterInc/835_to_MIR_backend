"""Isolated synthetic regression run; never use the configured production DB.

Run with the backend virtualenv: python scripts/run_phase1_checks.py --report PATH
"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
LABELS = [
    "edi835.test_transfer_api",
    "edi835.test_intake",
    "edi835.test_intake_duplicates",
    "edi835.test_sftp_intake",
    "edi835.test_sftp_invalid_file", "edi835.test_sftp_automation",
    "edi835.test_sftp_sequential_hhss", "edi835.test_sftp_source_independence",
    "edi835.test_intake_recovery",
    "edi835.test_intake_postgres",
    "edi835.test_phase1_baseline", "edi835.test_phase1_controls",
    "project835.tests_csrf", "project835.test_release_requests",
    "project835.test_release_configuration", "project835.test_database_guard",
    "project835.test_demo_access", "project835.test_drf_contract", "accounts.tests",
    "admin_panel.test_client_role_creation", "admin_panel.test_security_review",
    "edi835.test_listing_query_efficiency", "edi835.test_tracked_file_performance_api",
    "edi835.test_client_user_portal", "edi835.test_client_portal_financials",
    "edi835.test_client_portal_period", "edi835.test_client_portal_analysis",
    "edi835.test_analysis_queries", "edi835.test_client_portal_conversion",
    "edi835.test_full_sftp_combined_835", "edi835.test_storage",
    "edi835.test_client_portal_actions", "edi835.test_client_portal_835_837",
    "edi835.test_client_claim_slice", "edi835.test_client_claim_sources",
    "edi835.test_conversion_download_summary", "edi835.test_edi837_service",
    "admin_panel.mir_mapper_logic.test_financial_validation",
    "admin_panel.mir_mapper_logic.test_business_rules",
    "admin_panel.mir_mapper_logic.test_held_claims",
]


def git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = []
    for folder in ("accounts", "admin_panel", "converter", "edi835", "project835", "scripts"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".835"})
    paths.extend(ROOT / name for name in (
        "localsettings.py", "validation.py", "req.txt", "docs/phase1_acceptance.json",
    ))
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--label", action="append", help="Run selected test labels instead of the full regression suite.")
    parser.add_argument("--temp-dir", type=Path, default=ROOT / ".test-temp")
    args = parser.parse_args()
    args.temp_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    os.environ.update(
        SECRET_KEY="phase1-synthetic-test-key-only", DATABASE_URL="sqlite:///:memory:",
        ALLOW_SQLITE_FOR_TESTS="1", DJANGO_SETTINGS_MODULE="project835.test_settings",
        DEBUG="True",
    )
    import django
    django.setup()
    from django.conf import settings
    from django.test.runner import DiscoverRunner

    class EvidenceRunner(DiscoverRunner):
        result = None

        def suite_result(self, suite, result, **kwargs):
            self.result = result
            return super().suite_result(suite, result, **kwargs)

    settings.ROOT_URLCONF = "project835.urls"
    settings.MFA_ENFORCEMENT_ENABLED = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="phase1-", dir=args.temp_dir) as directory:
        previous_temp = tempfile.tempdir
        tempfile.tempdir = directory
        settings.MEDIA_ROOT = str(Path(directory) / "media")
        settings.INTAKE_STORAGE_ROOT = str(Path(directory) / "private-intake")
        try:
            runner = EvidenceRunner(verbosity=1, interactive=False)
            failures = runner.run_tests(args.label or LABELS)
        finally:
            tempfile.tempdir = previous_temp
    result = runner.result
    report = {
        "schema_version": 1, "environment": "isolated SQLite, synthetic fixtures",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": git_output("rev-parse", "HEAD"),
        "working_tree_changed": bool(git_output("status", "--porcelain")),
        "source_fingerprint_sha256": source_fingerprint(),
        "test_modules": args.label or LABELS,
        "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "duration_seconds": round(time.monotonic() - started, 3),
        "successful": result.wasSuccessful(),
        "production_approval": False,
        "limitations": "No PostgreSQL, AWS, partner, load, trust or backup/restore certification.",
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
