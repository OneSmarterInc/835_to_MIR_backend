from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("edi835", "0039_mpl_notice_msg_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="extracted_claim_numbers",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="mplnotice",
            name="ai_response",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="mplnotice",
            name="ai_suggestions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
