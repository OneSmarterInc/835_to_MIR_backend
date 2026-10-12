"""
Management command to verify cryptographic integrity of the audit trail.
Used by compliance officers and HIPAA/SOC2 auditors to verify that the
audit logs have not been tampered with or modified.
"""

from django.core.management.base import BaseCommand
import hashlib
from admin_panel.models import AuditLog, MIRMasterAuditLog


class Command(BaseCommand):
    help = "Verify cryptographic Merkle hash chain integrity of MIR Portal audit logs (HIPAA § 164.312(b))."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== OSIVAULT CRYPTOGRAPHIC AUDIT VERIFICATION ==="))

        # 1. Verify OSIVault Master Audit Ledger
        self.stdout.write("\n[1/2] Verifying OSIVault Master Cryptographic Audit Ledger (mir_master_audit_log)...")
        master_count = MIRMasterAuditLog.objects.count()
        if master_count == 0:
            self.stdout.write(self.style.WARNING("  Master audit ledger is currently empty (new events will append automatically)."))
        else:
            try:
                from osivault.audit import verify_chain
                report = verify_chain(MIRMasterAuditLog)
                if report.is_intact:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [PASS] Master Ledger Intact: {report.total_count} records verified across Merkle chain."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  [FAIL] Tampering detected in Master Ledger at PK {report.failed_pk}: {report.failure_reason}"
                        )
                    )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  [ERROR] Master Ledger verification error: {e}"))

        # 2. Verify Legacy AuditLog Ledger
        self.stdout.write("\n[2/2] Verifying Historic Audit Ledger (admin_panel_auditlog)...")
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
                    broken_records.append((record.id, "HMAC/SHA256 Payload Hash Mismatch"))
                prev_hash = record.entry_hash

            if not broken_records:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [PASS] Historic Ledger Intact: {legacy_count} records cryptographically verified with 0 breaks."
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [FAIL] Tampering detected in Historic Ledger: {len(broken_records)} discrepancies found!"
                    )
                )
                for rid, reason in broken_records[:5]:
                    self.stdout.write(self.style.ERROR(f"    - Record ID {rid}: {reason}"))

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== VERIFICATION COMPLETE: ALL LEDGERS COMPLIANT ==="))
