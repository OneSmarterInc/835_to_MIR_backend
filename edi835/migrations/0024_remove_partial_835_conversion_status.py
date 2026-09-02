from django.db import migrations, models


def mark_legacy_partial_runs_as_error(apps, schema_editor):
    EDI835File = apps.get_model("edi835", "EDI835File")
    EDI835File.objects.filter(status="PARTIAL").update(status="ERROR")


class Migration(migrations.Migration):
    dependencies = [("edi835", "0023_mirserviceline_allowed_amount")]

    operations = [
        migrations.RunPython(mark_legacy_partial_runs_as_error, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="edi835file",
            name="status",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"),
                    ("PROCESSING", "Processing"),
                    ("COMPLETED", "Completed"),
                    ("ARCHIVED", "Archived"),
                    ("ERROR", "Error"),
                ],
                default="UPLOADED",
                help_text="Current processing state.",
                max_length=50,
            ),
        ),
    ]
