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
    def test_partner_concurrency_limit_is_shared_across_clients(self):
        from .partner_management import create_partner, change_partner
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            clients = [Client.objects.create(client_code=f"QUOTA-{i}", name="Synthetic", status="ACTIVE") for i in range(2)]
            partner = create_partner(client=clients[0], name="Shared quota", workflows=["837"], mode="TOKEN",
                routing_required=False, actor="synthetic", reason="Concurrency acceptance")
            change_partner(partner.pk, action="CLIENTS", actor="synthetic", reason="Concurrency acceptance",
                data={"client_ids": [str(c.pk) for c in clients]})
            change_partner(partner.pk, action="PERMISSIONS", actor="synthetic", reason="Concurrency acceptance",
                data={"permissions": [{"action": "SUBMIT", "workflow": "837"}]})
            partner.concurrent_jobs = 1
            partner.save()
            receipts = [register_sources(client=c, actor=None, partner=partner, channel="PARTNER", workflow="837",
                sources=[("source", io.BytesIO(b"synthetic source"))], asynchronous=True)[0][0] for c in clients]
            barrier = threading.Barrier(2)

            def claim(receipt):
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    return claim_job(str(receipt.dispatch_id)) is not None
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, receipts))
            self.assertEqual(sum(results), 1)
            self.assertEqual(ProcessingAttempt.objects.filter(state="RUNNING").count(), 1)

    def test_rotated_credentials_share_one_submission_namespace(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import PartnerCredential, InboundTransfer, SubmissionKey
        from .partner_management import create_partner, change_partner
        from .transfer_service import key_digest, submit, SubmissionPending
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            tenant = Client.objects.create(client_code="PG-PARTNER", name="Synthetic", status="ACTIVE")
            partner = create_partner(client=tenant, name="Rotation", workflows=["837"], mode="TOKEN",
                routing_required=False, actor="synthetic", reason="Concurrency acceptance")
            change_partner(partner.pk, action="PERMISSIONS", actor="synthetic", reason="Concurrency acceptance",
                data={"permissions": [{"action": "SUBMIT", "workflow": "837"}]})
            credentials = [change_partner(partner.pk, action="TOKEN", actor="synthetic", reason="Concurrency acceptance")
                           ["credential_id"] for _ in range(2)]
            barrier = threading.Barrier(2)
            digest = key_digest("same-partner-key", partner=partner)

            def send(identifier, wait=True):
                close_old_connections()
                try:
                    credential = PartnerCredential.objects.select_related("partner").get(pk=identifier)
                    if wait:
                        barrier.wait(timeout=10)
                    receipt, _ = submit(client=tenant, user=None, partner=credential.partner,
                        partner_credential=credential, workflow="837",
                        upload=SimpleUploadedFile("synthetic", b"identical source"), digest=digest)
                    return str(receipt.pk)
                except SubmissionPending:
                    return None
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(send, credentials))
            canonical = send(credentials[1], wait=False)
            self.assertEqual({value for value in results if value}, {canonical})
            self.assertEqual(InboundTransfer.objects.count(), 1)
            self.assertEqual(SubmissionKey.objects.count(), 1)

    def test_v1_concurrent_key_recovers_one_receipt(self):
        from accounts.models import User
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import SubmissionKey, InboundTransfer
        from .transfer_service import key_digest, submit, SubmissionPending
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            tenant = Client.objects.create(client_code="PG-KEY", name="Synthetic", status="ACTIVE")
            user = User.objects.create_user(email="pg-key@example.test", name="Test", mobile="+15550002222", is_staff=True)
            digest = key_digest("same-key", user=user)
            barrier = threading.Barrier(2)

            def send(wait=True):
                close_old_connections()
                try:
                    if wait:
                        barrier.wait(timeout=10)
                    receipt, _ = submit(client=tenant, user=user, partner=None, workflow="837",
                        upload=SimpleUploadedFile("source", b"exact bytes"), digest=digest)
                    return str(receipt.pk)
                except SubmissionPending:
                    return None
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: send(), range(2)))
            canonical = send(wait=False)
            self.assertEqual({item for item in results if item}, {canonical})
            self.assertEqual(SubmissionKey.objects.count(), 1)
            self.assertEqual(InboundTransfer.objects.count(), 1)

    def test_v1_concurrent_operator_retries_create_one_audit(self):
        from accounts.models import User
        from .models import TransferRetry
        from .transfer_service import retry_transfer
        from .batch_jobs import write_job
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            tenant = Client.objects.create(client_code="PG-RETRY", name="Synthetic", status="ACTIVE")
            user = User.objects.create_user(email="pg-retry@example.test", name="Test", mobile="+15550003333", is_staff=True, is_superuser=True)
            from .models import IntakeStaffGrant
            from django.utils import timezone
            from datetime import timedelta
            IntakeStaffGrant.objects.create(user=user, client=tenant, can_retry=True,
                approved_by=user, reason="Synthetic retry grant", expires_at=timezone.now()+timedelta(days=1))
            receipt = register_sources(client=tenant, actor=user, channel="STAFF", workflow="837",
                sources=[("source", io.BytesIO(b"source"))], asynchronous=True)[0][0]
            job = claim_job(str(receipt.dispatch_id))
            job.update(state="FAILED", result={"success": False})
            write_job(job)
            barrier = threading.Barrier(2)

            def retry():
                close_old_connections()
                try:
                    item = type(receipt).objects.get(pk=receipt.pk)
                    barrier.wait(timeout=10)
                    retry_transfer(item, user, "Dependency restored")
                    return True
                except ValueError:
                    return False
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: retry(), range(2)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(TransferRetry.objects.count(), 1)

    def test_simultaneous_cross_channel_submissions_have_one_canonical_job(self):
        from .models import InboundTransfer, BatchDispatch
        with tempfile.TemporaryDirectory() as directory, override_settings(
            INTAKE_STORAGE_ROOT=Path(directory) / "private", MEDIA_ROOT=Path(directory) / "media",
        ):
            client = Client.objects.create(client_code="PG-DUP", name="Synthetic", status="ACTIVE")
            barrier = threading.Barrier(3)

            def submit(channel):
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    receipt = register_sources(client=client, actor=None, channel=channel, workflow="837",
                        sources=[(channel, io.BytesIO(b"same complete source"))], asynchronous=True)[0][0]
                    return receipt.pk
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=3) as pool:
                identifiers = list(pool.map(submit, ["STAFF", "PARTNER", "SFTP"]))
            self.assertEqual(len(set(identifiers)), 3)
            self.assertEqual(InboundTransfer.objects.filter(canonical_transfer__isnull=True).count(), 1)
            self.assertEqual(InboundTransfer.objects.filter(state="DUPLICATE").count(), 2)
            self.assertEqual(BatchDispatch.objects.filter(state="QUEUED").count(), 1)

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
