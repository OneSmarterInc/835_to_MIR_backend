from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0021_edi835file_conversion_findings")]

    operations = [
        migrations.AlterField(
            model_name="reconfile",
            name="status",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"),
                    ("PROCESSING", "Processing"),
                    ("PROCESSED", "Processed"),
                    ("PARTIAL", "Partially processed"),
                    ("FAILED", "Failed"),
                ],
                default="UPLOADED",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reconfile",
            name="held_record_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="reconfile",
            name="parsing_findings",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="reconprocessingrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("PROCESSING", "Processing"),
                    ("COMPLETED", "Completed"),
                    ("PARTIAL", "Partially completed"),
                    ("FAILED", "Failed"),
                ],
                default="PROCESSING",
                max_length=20,
            ),
        ),
        *[
            migrations.AddField(
                model_name="reconclaim",
                name=name,
                field=models.DecimalField(max_digits=18, decimal_places=2, default=0),
            )
            for name in (
                "mir904_bluecard_fee",
                "mir905_aea",
                "mir907_amount",
                "mir908_amount",
                "mpl920_pca_fee",
            )
        ],
    ]
