import hashlib

from django.db import migrations, models


def hash_existing_entries(apps, schema_editor):
    AuditLog = apps.get_model("admin_panel", "AuditLog")
    previous = ""
    for entry in AuditLog.objects.order_by("id").iterator():
        payload = "|".join((previous, entry.module, entry.action, entry.details, entry.performed_by))
        entry.previous_hash = previous
        entry.entry_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        entry.save(update_fields=["previous_hash", "entry_hash"])
        previous = entry.entry_hash


def install_postgres_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        CREATE OR REPLACE FUNCTION reject_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.client_id IS NOT NULL AND NEW.client_id IS NULL
               AND OLD.module = NEW.module AND OLD.action = NEW.action
               AND OLD.details = NEW.details AND OLD.performed_by = NEW.performed_by
               AND OLD.timestamp = NEW.timestamp AND OLD.previous_hash = NEW.previous_hash
               AND OLD.entry_hash = NEW.entry_hash THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Audit log entries are immutable';
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS audit_log_immutable ON admin_panel_auditlog;
        CREATE TRIGGER audit_log_immutable
        BEFORE UPDATE OR DELETE ON admin_panel_auditlog
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation();
    """)


def remove_postgres_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON admin_panel_auditlog;")
        schema_editor.execute("DROP FUNCTION IF EXISTS reject_audit_log_mutation();")


class Migration(migrations.Migration):
    dependencies = [("admin_panel", "0011_split_filename_and_user_onboarding_steps")]

    operations = [
        migrations.AddField(model_name="auditlog", name="previous_hash", field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="auditlog", name="entry_hash", field=models.CharField(blank=True, default="", editable=False, max_length=64)),
        migrations.RunPython(hash_existing_entries, migrations.RunPython.noop),
        migrations.AlterField(model_name="auditlog", name="entry_hash", field=models.CharField(editable=False, max_length=64, unique=True)),
        migrations.RunPython(install_postgres_guard, remove_postgres_guard),
    ]
