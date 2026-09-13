import uuid

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_preserve_837_filename_database_default"),
        ("edi835", "0044_normalize_835_claims"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClaimAlertEmail",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("category", models.CharField(choices=[("CONVERSION_HOLD", "Conversion hold"), ("MISSING_REFERENCE", "Missing 837 / RECON")], db_index=True, max_length=32)),
                ("alert_date", models.DateField(help_text="Eastern-calendar date for this daily consolidated alert.")),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("SENT", "Sent"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=16)),
                ("subject", models.CharField(max_length=500)),
                ("recipients", models.JSONField(blank=True, default=list)),
                ("claims", models.JSONField(blank=True, default=list)),
                ("body_text", models.TextField(blank=True, default="")),
                ("sent_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="claim_alert_emails", to="accounts.client")),
            ],
            options={
                "db_table": "claim_alert_email",
                "ordering": ["-sent_at", "-created_at"],
                "indexes": [
                    models.Index(fields=["client", "-sent_at"], name="claim_alert_client_sent_idx"),
                    models.Index(fields=["category", "-sent_at"], name="claim_alert_category_sent_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("client", "category", "alert_date"), name="uniq_claim_alert_client_category_day"),
                ],
            },
        ),
    ]
