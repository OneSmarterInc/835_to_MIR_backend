from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("edi835", "0024_remove_partial_835_conversion_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReconciliationReviewAction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope_key", models.CharField(db_index=True, max_length=64)),
                ("claim_control_number", models.CharField(max_length=100)),
                ("action_status", models.CharField(choices=[("YET_TO_START", "Yet to Start"), ("IN_PROCESS", "In Process"), ("HOLD", "Hold"), ("REJECTED", "Rejected"), ("APPROVED", "Approved")], default="YET_TO_START", max_length=20)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_review_actions", to="accounts.client")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_reconciliation_review_actions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "reconciliation_review_action",
                "constraints": [models.UniqueConstraint(fields=("scope_key", "claim_control_number"), name="uniq_reconciliation_review_action")],
            },
        ),
    ]
