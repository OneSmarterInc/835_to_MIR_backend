from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("edi835", "0052_client_portal_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientportalaction", name="created_by_label",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AlterField(
            model_name="clientportalaction", name="created_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="portal_actions", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
