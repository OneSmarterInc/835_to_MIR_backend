from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("edi835", "0042_mpl_notice_ai_response_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="normalized_email",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
