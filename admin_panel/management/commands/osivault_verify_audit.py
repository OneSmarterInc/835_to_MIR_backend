"""
Management command to verify cryptographic integrity of the audit trail.
Used by compliance officers, HIPAA/SOC2 auditors, and CI to verify that
audit logs, checkpoints, and cutover seals have not been tampered with.
"""

import hashlib
import sys
from django.core.management.base import BaseCommand, CommandError
from admin_panel.models import AuditLog, MIRMasterAuditLog
from admin_panel.audit_digest import compute_legacy_chain_digest


class Command(BaseCommand):
    help = "Verify cryptographic HMAC hash chain integrity, signed checkpoints, and cutover digest of MIR Portal audit logs (HIPAA § 164.312(b))."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== OSIVAULT CRYPTOGRAPHIC AUDIT VERIFICATION ==="))

        chain_ok = False
        checkpoint_ok = False
        digest_ok = False

        # 1. Verify OSIVault Master Audit Ledger (mir_master_audit_log)
        self.stdout.write("\n[1/3] Verifying OSIVault Master Cryptographic Audit Ledger (mir_master_audit_log)...")
        master_count = MIRMasterAuditLog.objects.count()
        if master_count == 0:
            self.stdout.write(self.style.WARNING("  Master audit ledger is currently empty (new events will append automatically)."))
            chain_ok = True
        else:
            try:
                from osivault.audit import verify_chain
                report = verify_chain(MIRMasterAuditLog)
                if report.is_intact:
                    chain_ok = True
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [PASS] Master Ledger Intact: {report.total_count} records verified across HMAC hash chain."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  [FAIL] Tampering detected in Master Ledger at PK {report.failed_pk}: {report.failure_reason}"
                        )
                    )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  [FAIL] Master Ledger verification error: {e}"))

        # 2. Verify OSIVault Signed Periodic Checkpoints
        self.stdout.write("\n[2/3] Verifying OSIVault Signed Periodic Checkpoints (osivault_audit_checkpoint)...")
        try:
            from osivault.audit.models import OSIVaultAuditCheckpoint
            from osivault.audit import verify_checkpoint

            checkpoints = OSIVaultAuditCheckpoint.objects.filter(
                table_name=MIRMasterAuditLog._meta.db_table
            ).order_by("-pk")
            cp_count = checkpoints.count()

            if cp_count == 0:
                self.stdout.write(self.style.WARNING("  No checkpoints recorded for master ledger yet."))
                checkpoint_ok = True
            else:
                latest_cp = checkpoints.first()
                cp_report = verify_checkpoint(latest_cp, MIRMasterAuditLog)
                if cp_report.is_valid:
                    checkpoint_ok = True
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [PASS] Checkpoint Verified: Latest checkpoint (PK {latest_cp.pk}, rows: {latest_cp.row_count}) MAC and state intact."
                        )
                    )
                else:
                    reason_msg = getattr(cp_report, "failure_reason", getattr(cp_report, "reason", "State mismatch"))
                    self.stdout.write(
                        self.style.ERROR(
                            f"  [FAIL] Checkpoint verification failed: {reason_msg} (PK {latest_cp.pk}, rows: {latest_cp.row_count}, live rows: {master_count})."
                        )
                    )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  [FAIL] Checkpoint verification error: {e}"))

        # 3. Verify Legacy Cutover Row and SHA-256 Digest Match
        self.stdout.write("\n[3/3] Verifying Sealed Legacy Cutover Digest (AUDIT_LEDGER_CUTOVER)...")
        cutover_row = (
            MIRMasterAuditLog.objects.filter(action="AUDIT_LEDGER_CUTOVER")
            .order_by("pk")
            .first()
        )
        lowest_row = MIRMasterAuditLog.objects.order_by("pk").first()

        if not cutover_row:
            self.stdout.write(self.style.ERROR("  [FAIL] Cutover row missing: No AUDIT_LEDGER_CUTOVER entry found."))
        elif lowest_row and cutover_row.pk != lowest_row.pk:
            self.stdout.write(
                self.style.ERROR(
                    f"  [FAIL] Cutover row is not the lowest PK in master ledger (cutover PK {cutover_row.pk} != lowest PK {lowest_row.pk})."
                )
            )
        else:
            live_hashes = list(AuditLog.objects.order_by("id").values_list("entry_hash", flat=True))
            live_digest, live_count, live_last = compute_legacy_chain_digest(live_hashes)

            new_vals = cutover_row.new_values or {}
            sealed_digest = new_vals.get("legacy_chain_digest")
            sealed_count = new_vals.get("legacy_row_count")
            sealed_last = new_vals.get("legacy_last_entry_hash")

            if (
                sealed_digest == live_digest
                and sealed_count == live_count
                and sealed_last == live_last
            ):
                digest_ok = True
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [PASS] Legacy Cutover Digest Intact: {live_count} rows match sealed SHA-256 digest ({live_digest[:16]}...)."
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [FAIL] Tampering detected in legacy table: live digest ({live_digest[:16]}..., count {live_count}, last {live_last[:16]}...) does not match sealed cutover ({str(sealed_digest)[:16]}..., count {sealed_count}, last {str(sealed_last)[:16]}...)."
                    )
                )

        # 4. Informational only: Historic AuditLog Ledger Walk (does not affect exit code)
        self.stdout.write("\n[Informational] Walking Historic Audit Ledger (admin_panel_auditlog)...")
        legacy_count = AuditLog.objects.count()
        if legacy_count == 0:
            self.stdout.write(self.style.WARNING("  Historic audit log is empty."))
        else:
            broken_records = []
            prev_hash = ""
            for record in AuditLog.objects.order_by("id"):
                if record.previous_hash != prev_hash:
                    broken_records.append((record.id, "Chain Link Discontinuity"))
                payload = "|".join((
                    record.previous_hash or "",
                    record.module or "",
                    record.action or "",
                    record.details or "",
                    record.performed_by or "",
                ))
                calc = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if calc != record.entry_hash:
                    broken_records.append((record.id, "Legacy Payload Hash Mismatch"))
                prev_hash = record.entry_hash

            if not broken_records:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [INFO] Historic Ledger Walk: {legacy_count} records traversed with 0 internal breaks."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"  [INFO] Historic Ledger Walk found {len(broken_records)} discrepancies."
                    )
                )

        # Exit code evaluation
        if not (chain_ok and checkpoint_ok and digest_ok):
            failed_parts = []
            if not chain_ok:
                failed_parts.append("Master Ledger HMAC Chain")
            if not checkpoint_ok:
                failed_parts.append("Signed Checkpoint")
            if not digest_ok:
                failed_parts.append("Sealed Legacy Cutover Digest")
            err_msg = f"Audit verification failed for: {', '.join(failed_parts)}"
            self.stdout.write(self.style.ERROR(f"\n=== VERIFICATION FAILED: {err_msg} ==="))
            raise CommandError(err_msg)

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== VERIFICATION COMPLETE: ALL LEDGERS COMPLIANT ==="))
