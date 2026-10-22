"""Synthetic SFTP boundary tests; no partner connection or real claim data."""
import io
import stat
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import paramiko
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from .edi837_service import ingest_837
from .intake_service import register_sources, finish_inline
from .intake_storage import store_stream
from .models import InboundTransfer, SFTPSourceClaim
from .sftp_intake import GuardedSFTP, SourceNotReady, _session, intake_session
from .sftp_security import verified_ssh_client
from .test_edi837_service import SAMPLE_837
from . import test_intake


CREDENTIALS = {"host": "sftp.example.test", "username": "synthetic", "port": 22}


class FakeSFTP:
    def __init__(self, files):
        self.files = dict(files)
        self.mtime = 100
        self.removed = []

    def normalize(self, path):
        return path

    def lstat(self, path):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                               st_size=len(self.files[path.rsplit("/", 1)[-1]]), st_mtime=self.mtime)

    stat = lstat

    def listdir_attr(self, folder):
        entries = []
        for name in self.files:
            attr = self.lstat(folder + "/" + name)
            attr.filename = name
            entries.append(attr)
        return entries

    def open(self, path, mode):
        return io.BytesIO(self.files[path.rsplit("/", 1)[-1]])

    def remove(self, path):
        del self.files[path.rsplit("/", 1)[-1]]
        self.removed.append(path)

    def close(self):
        pass


def age_observations():
    SFTPSourceClaim.objects.filter(state="OBSERVING").update(stable_since=timezone.now() - timedelta(minutes=2))


class SFTPIntakeTests(TestCase):
    setUp = test_intake.IntakeTests.setUp

    def guard(self, raw=None):
        token = _session.set(uuid.uuid4())
        self.addCleanup(_session.reset, token)
        return GuardedSFTP(raw or FakeSFTP({"source.837": SAMPLE_837.encode()}), CREDENTIALS, self.tenant)

    def ready(self, guard):
        guard.listdir_attr("/in")
        age_observations()

    def test_first_poll_defers_and_second_stable_poll_is_ready(self):
        guard = self.guard()
        self.assertEqual(guard.listdir_attr("/in"), [])
        age_observations()
        self.assertEqual(len(guard.listdir_attr("/in")), 1)
        self.assertFalse(InboundTransfer.objects.exists())

    def test_size_or_mtime_change_restarts_stability_window(self):
        guard = self.guard()
        self.ready(guard)
        guard.raw.mtime += 1
        self.assertEqual(guard.listdir_attr("/in"), [])
        age_observations()
        guard.raw.files["source.837"] += b"x"
        self.assertEqual(guard.listdir_attr("/in"), [])

    def test_empty_neighbor_does_not_block_ready_file(self):
        guard = self.guard(FakeSFTP({"empty.837": b"", "ready.837": b"synthetic"}))
        self.ready(guard)
        self.assertEqual([item.filename for item in guard.listdir_attr("/in")], ["ready.837"])

    def test_partial_names_and_symlinks_never_enter_intake(self):
        guard = self.guard(FakeSFTP({"a.part": b"one", ".hidden": b"one", "a.uploading": b"one"}))
        self.assertEqual(guard.listdir_attr("/in"), [])
        with self.assertRaises(SourceNotReady):
            guard.open("/in/a.part")
        with patch.object(guard.raw, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
            with self.assertRaises(SourceNotReady):
                guard.open("/in/link")

    def test_direct_selected_file_also_requires_stability(self):
        guard = self.guard()
        with self.assertRaises(SourceNotReady):
            guard.open("/in/source.837")

    def test_second_adapter_cannot_claim_same_path(self):
        guard = self.guard()
        self.ready(guard)
        with guard.open("/in/source.837"):
            other = self.guard(guard.raw)
            with self.assertRaises(SourceNotReady):
                other.open("/in/source.837")

    def test_same_endpoint_path_cannot_cross_clients(self):
        guard = self.guard()
        self.ready(guard)
        other = GuardedSFTP(guard.raw, CREDENTIALS, self.other)
        with self.assertRaises(SourceNotReady):
            other.open("/in/source.837")

    def test_changing_during_read_creates_no_receipt(self):
        guard = self.guard()
        self.ready(guard)
        original = guard.raw.open

        def changing(*args):
            handle = original(*args)
            guard.raw.mtime += 1
            return handle

        with patch.object(guard.raw, "open", side_effect=changing):
            with self.assertRaises(SourceNotReady), guard.open("/in/source.837") as handle:
                register_sources(client=self.tenant, actor=self.admin, channel="SFTP", workflow="837",
                                 sources=[("source.837", handle)])
        self.assertEqual(InboundTransfer.objects.count(), 0)
        self.assertEqual(guard.raw.removed, [])

    def test_real_ingest_binds_receipt_and_deletes_only_after_success(self):
        guard = self.guard()
        self.ready(guard)
        with guard.open("/in/source.837") as handle:
            stored = store_stream(handle, self.tenant.pk, max_bytes=100000)
        with self.assertRaises(SourceNotReady):
            guard.remove("/in/source.837")
        record, duplicate = ingest_837(self.tenant, self.admin, "source.837", stored, import_mode="SFTP")
        self.assertFalse(duplicate)
        self.assertEqual(record.status, "PROCESSED")
        self.assertIsNotNone(SFTPSourceClaim.objects.get().receipt_id)
        guard.remove("/in/source.837")
        self.assertEqual(guard.raw.removed, ["/in/source.837"])
        self.assertEqual(SFTPSourceClaim.objects.get().state, "REMOVED")

    def accept(self, guard, workflow="837", success=True):
        self.ready(guard)
        with guard.open("/in/source.837") as handle:
            receipts, _ = register_sources(client=self.tenant, actor=self.admin, channel="SFTP",
                workflow=workflow, sources=[("source.837", handle)])
        finish_inline(receipts, success)
        return receipts[0]

    def test_failed_receipt_retains_source(self):
        guard = self.guard()
        self.accept(guard, success=False)
        with self.assertRaises(SourceNotReady):
            guard.remove("/in/source.837")
        self.assertEqual(guard.raw.removed, [])

    def test_validation_only_cannot_delete_source(self):
        guard = self.guard()
        self.accept(guard, workflow="VALIDATE_835")
        with self.assertRaises(SourceNotReady):
            guard.remove("/in/source.837")

    def test_835_requires_confirmed_output(self):
        guard = self.guard()
        self.accept(guard, workflow="835")
        with self.assertRaises(SourceNotReady):
            guard.remove("/in/source.837")

    def test_same_size_replacement_is_not_deleted(self):
        guard = self.guard()
        self.accept(guard)
        raw = guard.raw.files["source.837"]
        guard.raw.files["source.837"] = b"X" + raw[1:]
        with self.assertRaises(SourceNotReady):
            guard.remove("/in/source.837")
        self.assertEqual(guard.raw.removed, [])

    def test_expired_reader_is_fenced_but_new_reader_can_retry(self):
        guard = self.guard()
        self.ready(guard)
        with guard.open("/in/source.837") as handle:
            SFTPSourceClaim.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
            with self.assertRaises(SourceNotReady):
                store_stream(handle, self.tenant.pk, max_bytes=100000)
        other = self.guard(guard.raw)
        with other.open("/in/source.837") as handle:
            store_stream(handle, self.tenant.pk, max_bytes=100000)

    def test_accepted_source_never_replays_after_crash_or_lease_expiry(self):
        guard = self.guard()
        self.accept(guard)
        SFTPSourceClaim.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        other = self.guard(guard.raw)
        with self.assertRaises(SourceNotReady):
            other.open("/in/source.837")

    def test_session_exception_retains_stored_source_for_review(self):
        raw = FakeSFTP({"source.837": SAMPLE_837.encode()})

        @intake_session
        def run():
            guard = GuardedSFTP(raw, CREDENTIALS, self.tenant)
            self.ready(guard)
            with guard.open("/in/source.837") as handle:
                store_stream(handle, self.tenant.pk, max_bytes=100000)
            raise RuntimeError("simulated crash")

        with self.assertRaises(RuntimeError):
            run()
        self.assertEqual(SFTPSourceClaim.objects.get().state, "REVIEW")
        self.assertEqual(raw.removed, [])

    def test_expired_owner_cannot_bind_receipt(self):
        guard = self.guard()
        self.ready(guard)
        with guard.open("/in/source.837") as handle:
            stored = store_stream(handle, self.tenant.pk, max_bytes=100000)
        SFTPSourceClaim.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(SourceNotReady):
            register_sources(client=self.tenant, actor=self.admin, channel="SFTP", workflow="837",
                             sources=[("source.837", stored)])
        self.assertFalse(InboundTransfer.objects.exists())

    def test_source_claim_protects_unbound_private_object_from_orphan_audit(self):
        import json
        import os
        from django.core.management import call_command
        from .intake_storage import source_path
        guard = self.guard()
        self.ready(guard)
        with guard.open("/in/source.837") as handle:
            stored = store_stream(handle, self.tenant.pk, max_bytes=100000)
        os.utime(source_path(stored.key), (1, 1))
        output = io.StringIO()
        call_command("intake_audit", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["unreferenced_objects_older_than_grace"], 0)

    def test_admin_explicit_client_is_authorized_and_client_user_cannot_switch(self):
        from .batch_test_837_v2 import _selected_client
        self.admin.client = self.tenant
        request = SimpleNamespace(user=self.admin)
        self.assertEqual(_selected_client(request, {"client_id": str(self.other.pk)}), self.other)
        self.admin.is_superuser = False
        self.assertIsNone(_selected_client(request, {"client_id": str(self.other.pk)}))
        self.admin.is_staff = False
        self.assertIsNone(_selected_client(request, {"client_id": str(self.other.pk)}))
        self.assertEqual(_selected_client(request, {"client_id": str(self.tenant.pk)}), self.tenant)

    def test_relay_cleanup_failure_retains_delivered_file_and_prevents_replay(self):
        from .batch_test_837_v3 import _relay_837_for_test
        inbound = FakeSFTP({"source.837": SAMPLE_837.encode()})

        class Outbound(FakeSFTP):
            def putfo(self, handle, path, **kwargs):
                self.files[path.rsplit("/", 1)[-1]] = handle.read()

            def rename(self, source, target):
                self.files[target.rsplit("/", 1)[-1]] = self.files.pop(source.rsplit("/", 1)[-1])

        outbound = Outbound({})
        ssh = SimpleNamespace(close=lambda: None)

        def route(client, purpose):
            return None, {**CREDENTIALS, "remote_folder": purpose}, purpose

        def connect(paramiko_module, credentials):
            return ssh, inbound if credentials["remote_folder"] == "837_IN" else outbound

        prefix = "edi835.batch_test_837_v3."
        with patch(prefix + "resolve_admin_sftp_route", side_effect=route), \
                patch(prefix + "_open_sftp", side_effect=connect), \
                patch(prefix + "get_saved_837_filename_format", return_value="unused"), \
                patch(prefix + "resolve_837_filename_format", return_value="target.837"), \
                patch.object(inbound, "remove", side_effect=OSError("cleanup unavailable")):
            request = SimpleNamespace(user=self.admin)
            _relay_837_for_test(request, self.tenant)
            age_observations()
            result = _relay_837_for_test(request, self.tenant)
            self.assertFalse(result["success"])
            self.assertEqual(len(outbound.files), 1)
            self.assertEqual(InboundTransfer.objects.count(), 1)
            self.assertEqual(SFTPSourceClaim.objects.get().state, "REVIEW")
            _relay_837_for_test(request, self.tenant)
            self.assertEqual(len(outbound.files), 1)
            self.assertEqual(InboundTransfer.objects.count(), 1)


class HostKeyTests(SimpleTestCase):
    @override_settings(SFTP_KNOWN_HOSTS_FILE="")
    def test_unknown_host_is_rejected(self):
        ssh = verified_ssh_client(paramiko)
        try:
            with patch.object(ssh, "_log"), self.assertRaises(paramiko.SSHException):
                ssh._policy.missing_host_key(ssh, "untrusted.example.test", paramiko.RSAKey.generate(1024))
        finally:
            ssh.close()

    @override_settings(SFTP_KNOWN_HOSTS_FILE="/verified/known_hosts")
    def test_explicit_known_hosts_and_reject_policy_are_loaded(self):
        with patch.object(paramiko, "SSHClient") as factory:
            verified_ssh_client(paramiko)
            factory.return_value.load_host_keys.assert_called_once_with("/verified/known_hosts")
            policy = factory.return_value.set_missing_host_key_policy.call_args.args[0]
            self.assertIsInstance(policy, paramiko.RejectPolicy)
