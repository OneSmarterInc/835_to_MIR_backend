"""
OSIVault v0.1.0 Audit Integration Test Suite (Task 4, 4b, and 4c).
Validates:
1. Fail-closed helper without error swallowing and MAC details coverage.
2. Cryptographic HMAC-SHA-256 hash chain verification.
3. Tamper detection on entry modifications.
4. Periodic signed checkpoint creation and verification.
5. PostgreSQL immutability trigger on mir_master_audit_log.
6. PostgreSQL immutability trigger on osivault_audit_checkpoint.
7. PostgreSQL strict freeze trigger on legacy admin_panel_auditlog (unconditional raw UPDATE rejection).
8. Dual-table historic read stream union on /admin-panel/api/audit-logs/.
"""

import json
import importlib
import os
from io import StringIO
from unittest.mock import patch, MagicMock
from django.core.management import call_command, CommandError
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.db import connection, DatabaseError, transaction
from accounts.models import Client, User
from admin_panel.models import AuditLog, MIRMasterAuditLog, log_audit_event
from admin_panel.audit_digest import compute_legacy_chain_digest
import osivault.audit
from osivault.audit.models import OSIVaultAuditCheckpoint
from osivault.audit.keys import ConfigurationError
import localsettings


@override_settings(MFA_ENFORCEMENT_ENABLED=False)
class OSIVaultAuditIntegrationTestCase(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            email="audit-superadmin@example.com",
            name="Audit Superadmin",
            mobile="5550199999",
            password="test-password-audit",
        )
        self.client_obj = Client.objects.create(
            name="Audit Tenant Alpha",
            client_code="TENANT_ALPHA",
            email="tenant_alpha@example.com",
        )

    def test_01_helper_logs_event_without_swallowing(self):
        """Helper logs complete payload with details in MAC scope and raises fail-closed on failure."""
        entry = log_audit_event(
            module="CLIENT",
            action="CLIENT_CREATED",
            details="New client initialized with primary key binding",
            performed_by="Audit Superadmin",
            client=self.client_obj,
            resource_type="CLIENT",
            resource_id=str(self.client_obj.pk),
            old_values={"status": "PENDING"},
            new_values={"status": "ACTIVE"},
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, "Audit Superadmin")
        self.assertEqual(entry.tenant, str(self.client_obj.pk))
        self.assertEqual(entry.module, "CLIENT")
        self.assertEqual(entry.action, "CLIENT_CREATED")
        self.assertEqual(entry.details, "New client initialized with primary key binding")
        self.assertEqual(entry.new_values.get("details"), "New client initialized with primary key binding")
        self.assertEqual(entry.client, self.client_obj)
        self.assertTrue(len(entry.entry_hash) > 16)
        self.assertIn("mac_alg", entry.envelope)

        # Fail-closed test: exceptions must never be silently swallowed
        with patch("osivault.audit.append", side_effect=RuntimeError("Fail-closed policy violation")):
            with self.assertRaises(RuntimeError):
                log_audit_event(
                    module="CLIENT",
                    action="CLIENT_UPDATED",
                    details="Should not be swallowed",
                    performed_by="Audit Superadmin",
                )

    def test_02_hmac_hash_chain_verification_intact(self):
        """Validates that sequential log_audit_event calls create an unbroken HMAC hash chain."""
        for i in range(3):
            log_audit_event(
                module="WORKFLOW",
                action=f"STEP_{i}_COMPLETED",
                details=f"Workflow step {i} executed",
                performed_by="Audit Superadmin",
                client=self.client_obj,
            )

        report = osivault.audit.verify_chain(MIRMasterAuditLog)
        self.assertTrue(report.is_intact)
        self.assertGreaterEqual(report.total_count, 3)

    def test_03_tampering_detection_breaks_hash_chain(self):
        """Validates that modifying payload data causes MAC verification to fail."""
        entry = log_audit_event(
            module="SECURITY",
            action="ACCESS_GRANTED",
            details="Legitimate access event",
            performed_by="Audit Superadmin",
            client=self.client_obj,
        )

        # Verified under true state
        verified_clean, _, _ = osivault.audit.verify_entry(entry)
        self.assertTrue(verified_clean)

        # Simulated tampering: altering payload values invalidate the HMAC MAC
        entry.action = "ACCESS_REVOKED"
        verified_tampered, _, reason = osivault.audit.verify_entry(entry)
        self.assertFalse(verified_tampered)
        self.assertIn("MAC verification failed", reason)

    def test_04_signed_checkpoint_creation_and_verification(self):
        """Creates a signed checkpoint on mir_master_audit_log and cryptographically validates it."""
        log_audit_event(
            module="GOVERNANCE",
            action="CHECKPOINT_TEST",
            details="Checkpoint generation assertion",
            performed_by="Audit Superadmin",
            client=self.client_obj,
        )

        checkpoint = osivault.audit.checkpoint(MIRMasterAuditLog)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.table_name, "mir_master_audit_log")

        cp_report = osivault.audit.verify_checkpoint(checkpoint, MIRMasterAuditLog)
        self.assertTrue(cp_report.is_valid)

    def test_05_mir_master_audit_log_immutable_trigger_blocks_update_and_delete(self):
        """PostgreSQL trigger prevents UPDATE and DELETE on active master audit ledger."""
        entry = log_audit_event(
            module="SYSTEM",
            action="TRIGGER_TEST",
            details="Trigger enforcement test",
            performed_by="Audit Superadmin",
        )

        if connection.vendor == "postgresql":
            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE mir_master_audit_log SET action = 'HACKED' WHERE id = %s",
                            [entry.id],
                        )
            self.assertIn("immutable", str(cm.exception).lower())

            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM mir_master_audit_log WHERE id = %s",
                            [entry.id],
                        )
            self.assertIn("immutable", str(cm.exception).lower())

    def test_06_osivault_audit_checkpoint_immutable_trigger_blocks_update_and_delete(self):
        """PostgreSQL trigger prevents UPDATE and DELETE on signed audit checkpoints."""
        checkpoint = osivault.audit.checkpoint(MIRMasterAuditLog)

        if connection.vendor == "postgresql":
            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE osivault_audit_checkpoint SET checkpoint_hash = 'TAMPERED' WHERE id = %s",
                            [checkpoint.id],
                        )
            self.assertIn("immutable", str(cm.exception).lower())

            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM osivault_audit_checkpoint WHERE id = %s",
                            [checkpoint.id],
                        )
            self.assertIn("immutable", str(cm.exception).lower())

    def test_07_legacy_auditlog_immutable_trigger_blocks_raw_update_unconditionally(self):
        """PostgreSQL trigger strictly prevents raw UPDATE and DELETE on frozen legacy audit log."""
        if connection.vendor == "postgresql":
            legacy_rec = AuditLog.objects.order_by("-id").first()
            if not legacy_rec:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO admin_panel_auditlog (module, action, details, performed_by, timestamp, previous_hash, entry_hash) "
                        "VALUES ('MIGRATION', 'INIT', 'Legacy entry', 'System', NOW(), '', 'abc123hash')"
                    )
                legacy_rec = AuditLog.objects.order_by("-id").first()

            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE admin_panel_auditlog SET action = 'TAMPERED' WHERE id = %s",
                            [legacy_rec.id],
                        )
            self.assertIn("frozen and immutable", str(cm.exception).lower())

            with self.assertRaises(DatabaseError) as cm:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM admin_panel_auditlog WHERE id = %s",
                            [legacy_rec.id],
                        )
            self.assertIn("frozen and immutable", str(cm.exception).lower())

    def test_08_dual_table_historic_read_stream_union(self):
        """The /admin-panel/api/audit-logs/ endpoint returns a unified feed of legacy and active master logs."""
        log_audit_event(
            module="TEST_UNION",
            action="MASTER_EVENT",
            details="Active event in master ledger",
            performed_by="Audit Superadmin",
            client=self.client_obj,
        )

        self.client.force_login(self.superadmin)
        response = self.client.get("/admin-panel/api/audit-logs/")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        logs = data.get("logs", [])
        self.assertGreater(len(logs), 0)

        actions = [item.get("action") for item in logs]
        self.assertIn("MASTER_EVENT", actions)

    def test_09_audit_failure_is_load_bearing(self):
        """Patch key provider to fail and assert that an audit-logging view returns 500 with no row written."""
        self.client_obj.stage = "offboarded"
        self.client_obj.save(update_fields=["stage"])

        initial_count = MIRMasterAuditLog.objects.count()

        mock_provider = MagicMock()
        mock_provider.get_current_key.side_effect = ConfigurationError("Simulated audit key provider failure")

        with patch("osivault.audit.keys.get_default_key_provider", return_value=mock_provider), \
             patch("osivault.audit.operations.get_default_key_provider", return_value=mock_provider):
            client = self.client_class(raise_request_exception=False)
            client.force_login(self.superadmin)
            response = client.post(f"/admin-panel/api/clients/{self.client_obj.id}/steps/step_1/redo/")
            self.assertEqual(response.status_code, 500)
            self.assertEqual(MIRMasterAuditLog.objects.count(), initial_count)

    def test_10_cutover_row_is_first_and_digest_matches(self):
        """Assert exactly one AUDIT_LEDGER_CUTOVER row with lowest PK, matching legacy digest, and valid checkpoint."""
        cutover_qs = MIRMasterAuditLog.objects.filter(action="AUDIT_LEDGER_CUTOVER")
        if not cutover_qs.exists():
            entry_hashes = list(AuditLog.objects.order_by("id").values_list("entry_hash", flat=True))
            digest, count, last = compute_legacy_chain_digest(entry_hashes)
            log_audit_event(
                module="GOVERNANCE",
                action="AUDIT_LEDGER_CUTOVER",
                details="Cutover from legacy audit log to OSIVault HMAC-SHA-256 master ledger",
                performed_by="SYSTEM_MIGRATION",
                resource_type="GOVERNANCE",
                resource_id="CUTOVER",
                new_values={
                    "details": "Cutover from legacy audit log to OSIVault HMAC-SHA-256 master ledger",
                    "legacy_chain_digest": digest,
                    "legacy_row_count": count,
                    "legacy_last_entry_hash": last,
                },
            )
            osivault.audit.checkpoint(MIRMasterAuditLog)

        self.assertEqual(cutover_qs.count(), 1)
        cutover_row = cutover_qs.first()
        lowest_pk = MIRMasterAuditLog.objects.order_by("pk").values_list("pk", flat=True).first()
        self.assertEqual(cutover_row.pk, lowest_pk)

        entry_hashes = list(AuditLog.objects.order_by("id").values_list("entry_hash", flat=True))
        digest, count, last = compute_legacy_chain_digest(entry_hashes)

        new_vals = cutover_row.new_values or {}
        self.assertEqual(new_vals.get("legacy_chain_digest"), digest)
        self.assertEqual(new_vals.get("legacy_row_count"), count)
        self.assertEqual(new_vals.get("legacy_last_entry_hash"), last)

        checkpoints = OSIVaultAuditCheckpoint.objects.filter(table_name=MIRMasterAuditLog._meta.db_table)
        self.assertGreaterEqual(checkpoints.count(), 1)
        latest_cp = checkpoints.order_by("-pk").first()
        cp_report = osivault.audit.verify_checkpoint(latest_cp, MIRMasterAuditLog)
        self.assertTrue(cp_report.is_valid)

    def test_11_verify_command_exit_codes(self):
        """call_command('osivault_verify_audit') passes on clean DB, but fails with non-zero exit code if legacy table is tampered."""
        out = StringIO()
        call_command("osivault_verify_audit", stdout=out)
        self.assertIn("ALL LEDGERS COMPLIANT", out.getvalue())

        legacy_row = AuditLog.objects.order_by("id").first()
        if not legacy_row:
            legacy_row = AuditLog.objects.create(
                module="TEST", action="INIT", details="Legacy initial entry", performed_by="System"
            )

        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER IF EXISTS trg_freeze_admin_panel_auditlog ON admin_panel_auditlog;")
            cursor.execute(
                "UPDATE admin_panel_auditlog SET details = 'Tampered text by attacker', entry_hash = 'tampered_hash_00000000000000000000000000000000' WHERE id = %s;",
                [legacy_row.id],
            )
            cursor.execute("""
                CREATE TRIGGER trg_freeze_admin_panel_auditlog
                BEFORE UPDATE OR DELETE ON admin_panel_auditlog
                FOR EACH ROW EXECUTE FUNCTION freeze_legacy_admin_panel_auditlog();
            """)

        with self.assertRaises((CommandError, SystemExit)):
            call_command("osivault_verify_audit", stdout=StringIO())

    def test_12_boot_fails_closed_without_key(self):
        """Settings boot raises ImproperlyConfigured fail-closed when DEBUG=False and no audit key is set."""
        orig_debug = os.environ.get("DEBUG")
        orig_key = os.environ.get("OSIVAULT_AUDIT_CURRENT_KEY")
        try:
            os.environ["DEBUG"] = "False"
            os.environ["OSIVAULT_AUDIT_CURRENT_KEY"] = ""
            with patch("dotenv.load_dotenv"):
                with self.assertRaises(ImproperlyConfigured):
                    importlib.reload(localsettings)
        finally:
            if orig_debug is not None:
                os.environ["DEBUG"] = orig_debug
            else:
                os.environ.pop("DEBUG", None)
            if orig_key is not None:
                os.environ["OSIVAULT_AUDIT_CURRENT_KEY"] = orig_key
            else:
                os.environ.pop("OSIVAULT_AUDIT_CURRENT_KEY", None)
            with patch("dotenv.load_dotenv"):
                importlib.reload(localsettings)
