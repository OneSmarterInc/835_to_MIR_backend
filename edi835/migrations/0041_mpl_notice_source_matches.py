from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("edi835", "0040_mpl_notice_extraction_response"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="source_matches",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
