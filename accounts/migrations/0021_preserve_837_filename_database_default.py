from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0020_client_edi837_filename_format")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE client "
                        "ALTER COLUMN edi837_filename_format "
                        "SET DEFAULT 'YYYYMMDDhhmmss.837'"
                    ),
                    reverse_sql=(
                        "ALTER TABLE client "
                        "ALTER COLUMN edi837_filename_format DROP DEFAULT"
                    ),
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
