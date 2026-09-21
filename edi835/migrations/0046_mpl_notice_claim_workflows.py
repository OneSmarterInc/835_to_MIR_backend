from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("edi835", "0045_mpl_claim_workflow_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="mplnotice",
            name="claim_workflow_statuses",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
