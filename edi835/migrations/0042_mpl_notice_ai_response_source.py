from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("edi835", "0041_mpl_notice_source_matches"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="ai_response_source",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
