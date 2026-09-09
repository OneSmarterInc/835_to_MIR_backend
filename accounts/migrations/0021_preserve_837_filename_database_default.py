from django.db import migrations


def set_postgresql_filename_default(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "ALTER TABLE client "
        "ALTER COLUMN edi837_filename_format "
        "SET DEFAULT 'YYYYMMDDhhmmss.837'"
    )


def drop_postgresql_filename_default(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "ALTER TABLE client "
        "ALTER COLUMN edi837_filename_format DROP DEFAULT"
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0020_client_edi837_filename_format")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    set_postgresql_filename_default,
                    drop_postgresql_filename_default,
                ),
            ],
            state_operations=[
                migrations.RemoveField(
                    model_name="client",
                    name="edi837_filename_format",
                ),
            ],
        ),
    ]
