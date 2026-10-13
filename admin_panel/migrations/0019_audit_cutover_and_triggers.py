from django.db import migrations


def install_postgres_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        -- 1. Immutability trigger for mir_master_audit_log
        CREATE OR REPLACE FUNCTION osivault_prevent_immutability_mir_master_audit_log()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'OSIVault audit entries are immutable and cannot be updated or deleted.';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_osivault_immutable_mir_master_audit_log ON mir_master_audit_log;

        CREATE TRIGGER trg_osivault_immutable_mir_master_audit_log
        BEFORE UPDATE OR DELETE ON mir_master_audit_log
        FOR EACH ROW EXECUTE FUNCTION osivault_prevent_immutability_mir_master_audit_log();

        -- 2. Immutability trigger for osivault_audit_checkpoint
        CREATE OR REPLACE FUNCTION osivault_prevent_immutability_osivault_audit_checkpoint()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'OSIVault audit checkpoints are immutable and cannot be updated or deleted.';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_osivault_immutable_osivault_audit_checkpoint ON osivault_audit_checkpoint;

        CREATE TRIGGER trg_osivault_immutable_osivault_audit_checkpoint
        BEFORE UPDATE OR DELETE ON osivault_audit_checkpoint
        FOR EACH ROW EXECUTE FUNCTION osivault_prevent_immutability_osivault_audit_checkpoint();

        -- 3. Strict freeze trigger for legacy admin_panel_auditlog
        CREATE OR REPLACE FUNCTION freeze_legacy_admin_panel_auditlog()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.client_id IS NOT NULL AND NEW.client_id IS NULL
               AND OLD.module = NEW.module AND OLD.action = NEW.action
               AND OLD.details = NEW.details AND OLD.performed_by = NEW.performed_by
               AND OLD.timestamp = NEW.timestamp AND OLD.previous_hash = NEW.previous_hash
               AND OLD.entry_hash = NEW.entry_hash THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Historic AuditLog entries are permanently frozen and immutable.';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS audit_log_immutable ON admin_panel_auditlog;
        DROP TRIGGER IF EXISTS trg_freeze_admin_panel_auditlog ON admin_panel_auditlog;

        CREATE TRIGGER trg_freeze_admin_panel_auditlog
        BEFORE UPDATE OR DELETE ON admin_panel_auditlog
        FOR EACH ROW EXECUTE FUNCTION freeze_legacy_admin_panel_auditlog();
    """)


def remove_postgres_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        DROP TRIGGER IF EXISTS trg_osivault_immutable_mir_master_audit_log ON mir_master_audit_log;
        DROP FUNCTION IF EXISTS osivault_prevent_immutability_mir_master_audit_log();
        DROP TRIGGER IF EXISTS trg_osivault_immutable_osivault_audit_checkpoint ON osivault_audit_checkpoint;
        DROP FUNCTION IF EXISTS osivault_prevent_immutability_osivault_audit_checkpoint();
        DROP TRIGGER IF EXISTS trg_freeze_admin_panel_auditlog ON admin_panel_auditlog;
        DROP FUNCTION IF EXISTS freeze_legacy_admin_panel_auditlog();
    """)


def perform_cutover(apps, schema_editor):
    AuditLog = apps.get_model("admin_panel", "AuditLog")
    from admin_panel.models import MIRMasterAuditLog as ConcreteMasterLog
    import osivault.audit

    if not ConcreteMasterLog.objects.exists():
        last_legacy = AuditLog.objects.order_by("-id").first()
        legacy_digest = last_legacy.entry_hash if last_legacy else ""

        osivault.audit.append(
            model_class=ConcreteMasterLog,
            actor="SYSTEM_MIGRATION",
            tenant="SYSTEM",
            resource_type="GOVERNANCE",
            resource_id="CUTOVER",
            action="AUDIT_LEDGER_CUTOVER",
            old_values={
                "legacy_table": "admin_panel_auditlog",
                "legacy_records": AuditLog.objects.count(),
            },
            new_values={
                "legacy_digest": legacy_digest,
                "details": "Cutover from legacy audit log to OSIVault HMAC-SHA-256 master ledger",
            },
            module="GOVERNANCE",
            details="Cutover from legacy audit log to OSIVault HMAC-SHA-256 master ledger",
        )
        osivault.audit.checkpoint(ConcreteMasterLog)


class Migration(migrations.Migration):

    dependencies = [
        ('admin_panel', '0018_alter_mirmasterauditlog_client'),
        ('audit', '0002_alter_osivaultauditcheckpoint_previous_checkpoint_hash_and_more'),
    ]

    operations = [
        migrations.RunPython(install_postgres_guards, remove_postgres_guards),
        migrations.RunPython(perform_cutover, migrations.RunPython.noop),
    ]
