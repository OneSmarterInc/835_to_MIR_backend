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
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.db import connection, DatabaseError, transaction
from accounts.models import Client, User
from admin_panel.models import AuditLog, MIRMasterAuditLog, log_audit_event
import osivault.audit
from osivault.audit.models import OSIVaultAuditCheckpoint


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
