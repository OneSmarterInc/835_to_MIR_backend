from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0020_separate_sftp_automation_and_recon_folder")]

    operations = [
        migrations.AlterField(
            model_name="edi835file",
            name="status",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"), ("PROCESSING", "Processing"),
                    ("COMPLETED", "Completed"), ("ARCHIVED", "Archived"),
                    ("PARTIAL", "Partially delivered"), ("ERROR", "Error"),
                ],
                default="UPLOADED", help_text="Current processing state.", max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="edi835file", name="conversion_findings",
            field=models.JSONField(blank=True, default=list, help_text="Claim-level conversion findings and hold reasons."),
        ),
        migrations.AddField(
            model_name="edi835file", name="delivered_claims_count",
            field=models.IntegerField(default=0, help_text="Claims delivered in the MIR output."),
        ),
        migrations.AddField(
            model_name="edi835file", name="held_claims_count",
            field=models.IntegerField(default=0, help_text="Claims retained because conversion findings require review."),
        ),
    ]
