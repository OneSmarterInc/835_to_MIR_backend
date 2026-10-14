from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("edi835", "0051_mpl_python_evidence_definitions"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientPortalAction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[
                    ("NOTE", "Add note"), ("REQUEST_RESEND", "Request source file resend"),
                    ("REQUEST_PLAN_ID", "Request missing plan ID correction"),
                    ("REQUEST_DICTIONARY", "Request code dictionary correction"),
                    ("REQUEST_RELEASE", "Request exception review / release"),
                    ("IGNORE_FILE", "Hide file issue"), ("REOPEN_FILE", "Reopen file issue"),
                ], max_length=32)),
                ("message", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("REVIEWED", "Reviewed")], default="OPEN", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="portal_actions", to="accounts.client")),
                ("file", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="portal_actions", to="edi835.edi835file")),
                ("claim", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="portal_actions", to="edi835.edi835claim")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portal_actions", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_portal_actions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "client_portal_action", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(model_name="clientportalaction", index=models.Index(fields=["client", "-created_at"], name="client_portal_activity_idx")),
        migrations.AddIndex(model_name="clientportalaction", index=models.Index(fields=["client", "status"], name="client_portal_status_idx")),
    ]
