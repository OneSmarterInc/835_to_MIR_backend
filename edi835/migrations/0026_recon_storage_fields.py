from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0025_reconciliation_review_action")]

    operations = [
        migrations.AddField(
            model_name="reconfile",
            name="file_kind",
            field=models.CharField(
                choices=[("RECON", "RECON"), ("837", "837 Reference")],
                db_index=True,
                default="RECON",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="reconfile",
            name="archive_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
