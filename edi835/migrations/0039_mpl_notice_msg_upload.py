from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("edi835", "0038_mpl_notice_analysis"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="source_filename",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="mplnotice",
            name="source_content_type",
            field=models.CharField(
                blank=True,
                default="application/vnd.ms-outlook",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="mplnotice",
            name="source_file",
            field=models.BinaryField(blank=True, null=True),
        ),
    ]
