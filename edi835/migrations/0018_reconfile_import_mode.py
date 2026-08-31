from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0017_datatable_preview_content")]

    operations = [
        migrations.AddField(
            model_name="reconfile",
            name="import_mode",
            field=models.CharField(
                choices=[("MANUAL", "Manual"), ("SFTP", "SFTP")],
                default="MANUAL",
                help_text="How this RECON file entered the system: MANUAL or SFTP.",
                max_length=20,
            ),
        ),
    ]
