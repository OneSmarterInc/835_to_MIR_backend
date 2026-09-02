from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("admin_panel", "0012_immutable_audit_log"),
    ]
    operations = [
        migrations.CreateModel(
            name="AdminClientAccessGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.TextField()),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("administrator", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="client_access_grants", to=settings.AUTH_USER_MODEL)),
                ("approved_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="approved_client_access_grants", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="administrator_access_grants", to="accounts.client")),
            ],
        ),
        migrations.AddIndex(
            model_name="adminclientaccessgrant",
            index=models.Index(fields=["administrator", "client", "expires_at"], name="admin_panel_adminis_725c13_idx"),
        ),
    ]
