from pathlib import Path

from django.conf import settings
from django.db import migrations, models


def ensure_input_content_column(apps, schema_editor):
    EDI835File = apps.get_model("edi835", "EDI835File")
    table_name = EDI835File._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(cursor, table_name)
        }
    if "input_file_content" not in columns:
        field = models.TextField(
            blank=True,
            default="",
            help_text="Database copy of the original 835/X12 input content.",
        )
        field.set_attributes_from_name("input_file_content")
        schema_editor.add_field(EDI835File, field)


def backfill_input_content(apps, schema_editor):
    EDI835File = apps.get_model("edi835", "EDI835File")
    for record in EDI835File.objects.filter(input_file_content="").iterator():
        for relative_path in (record.archive_path, record.input_path):
            if not relative_path:
                continue
            path = Path(settings.BASE_DIR) / relative_path
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
            except OSError:
                continue
            if content:
                record.input_file_content = content
                record.save(update_fields=["input_file_content"])
                break


class Migration(migrations.Migration):
    dependencies = [("edi835", "0016_recon_global_system_scope")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(ensure_input_content_column, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="edi835file",
                    name="input_file_content",
                    field=models.TextField(
                        blank=True,
                        default="",
                        help_text="Database copy of the original 835/X12 input content.",
                    ),
                ),
            ],
        ),
        migrations.RunPython(backfill_input_content, migrations.RunPython.noop),
    ]
