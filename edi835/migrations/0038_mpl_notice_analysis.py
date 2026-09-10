import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_preserve_837_filename_database_default"),
        ("edi835", "0037_allow_duplicate_recon_intakes"),
    ]

    operations = [
        migrations.CreateModel(
            name="MPLNotice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("subject", models.CharField(max_length=500)),
                ("sender_text", models.CharField(blank=True, default="", max_length=255)),
                ("received_at", models.DateTimeField(blank=True, null=True)),
                ("reporting_year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("reporting_period_start", models.DateField(blank=True, null=True)),
                ("reporting_period_end", models.DateField(blank=True, null=True)),
                ("program", models.CharField(blank=True, default="", max_length=30)),
                ("notice_type", models.CharField(blank=True, default="", max_length=30)),
                ("raw_email_body", models.TextField()),
                ("requested_claim_numbers", models.JSONField(blank=True, default=list)),
                ("latest_message_body", models.TextField(blank=True, default="")),
                ("quoted_email_history", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("RECEIVED", "Received"), ("PARSING_EMAIL", "Parsing email"), ("MATCHING_CLAIMS", "Matching claims"), ("WAITING_FOR_CLAIM_SELECTION", "Waiting for claim selection"), ("COLLECTING_EVIDENCE", "Collecting evidence"), ("RUNNING_VALIDATIONS", "Running validations"), ("ANALYZING", "Analyzing"), ("COMPLETED", "Completed"), ("REVIEW_REQUIRED", "Review required"), ("FAILED", "Failed")], db_index=True, default="RECEIVED", max_length=40)),
                ("processing_started_at", models.DateTimeField(blank=True, null=True)),
                ("processing_completed_at", models.DateTimeField(blank=True, null=True)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="mpl_notices", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_mpl_notices", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "mpl_notice", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="MPLNoticeClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("matching_method", models.CharField(max_length=50)),
                ("matching_confidence", models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ("confirmed_by_user", models.BooleanField(default=False)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claim", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="mpl_notice_claims", to="edi835.edi837claim")),
                ("notice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notice_claims", to="edi835.mplnotice")),
            ],
            options={"db_table": "mpl_notice_claim"},
        ),
        migrations.CreateModel(
            name="MPLClaimAnalysis",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model_id", models.CharField(blank=True, default="", max_length=255)),
                ("prompt_version", models.CharField(default="mpl-3b-v1", max_length=30)),
                ("schema_version", models.CharField(default="1.0", max_length=30)),
                ("timeline", models.JSONField(blank=True, default=list)),
                ("findings", models.JSONField(blank=True, default=list)),
                ("recommended_actions", models.JSONField(blank=True, default=list)),
                ("related_files", models.JSONField(blank=True, default=list)),
                ("summary", models.TextField(blank=True, default="")),
                ("primary_issue_code", models.CharField(blank=True, default="", max_length=80)),
                ("needs_response", models.BooleanField(default=True)),
                ("confidence", models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ("raw_model_output", models.JSONField(blank=True, default=dict)),
                ("review_status", models.CharField(choices=[("PENDING", "Pending"), ("APPROVED", "Approved"), ("CHANGES_REQUIRED", "Changes required")], default="PENDING", max_length=30)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("notice_claim", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="analysis", to="edi835.mplnoticeclaim")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_mpl_analyses", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "mpl_claim_analysis"},
        ),
        migrations.AddIndex(model_name="mplnotice", index=models.Index(fields=["client", "-created_at"], name="mpl_notice_client_date_idx")),
        migrations.AddIndex(model_name="mplnotice", index=models.Index(fields=["status", "created_at"], name="mpl_notice_work_idx")),
        migrations.AddConstraint(model_name="mplnoticeclaim", constraint=models.UniqueConstraint(fields=("notice", "claim"), name="uniq_mpl_notice_claim")),
    ]
