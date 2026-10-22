"""Run against an isolated PostgreSQL test DB, never the production database."""
import io
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from accounts.models import Client
from .batch_jobs import claim_job
from .intake_service import register_sources
from .models import ProcessingAttempt


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks and separate connections")
class IntakePostgresConcurrencyTests(TransactionTestCase):
    def test_manual_and_scheduled_adapters_share_one_source_claim(self):
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        from .sftp_intake import GuardedSFTP, SourceNotReady, _session
        from .test_sftp_intake import FakeSFTP, CREDENTIALS
        from .models import SFTPSourceClaim

        client = Client.objects.create(client_code="PG-SFTP", name="Synthetic", status="ACTIVE")
        raw = FakeSFTP({"source.837": b"synthetic source"})
        token = _session.set(uuid.uuid4())
        try:
            GuardedSFTP(raw, CREDENTIALS, client).listdir_attr("/in")
        finally:
            _session.reset(token)
        SFTPSourceClaim.objects.update(stable_since=timezone.now() - timedelta(minutes=2))
        barrier = threading.Barrier(2)

        def claim():
            close_old_connections()
            token = _session.set(uuid.uuid4())
            try:
                guard = GuardedSFTP(raw, CREDENTIALS, client)
                barrier.wait(timeout=10)
                with guard.open("/in/source.837"):
                    return True
            except SourceNotReady:
                return False
            finally:
                _session.reset(token)
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(SFTPSourceClaim.objects.count(), 1)

    def test_two_workers_can_only_claim_one_execution(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            client = Client.objects.create(client_code="PG-INTAKE", name="Synthetic", status="ACTIVE")
            receipts, _ = register_sources(client=client, actor=None, channel="SYSTEM", workflow="837",
                                          sources=[("synthetic.837", io.BytesIO(b"synthetic"))], asynchronous=True)
            identifier = str(receipts[0].dispatch_id)
            barrier = threading.Barrier(2)

            def claim():
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    return claim_job(identifier)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: claim(), range(2)))
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(ProcessingAttempt.objects.filter(dispatch_id=identifier).count(), 1)
