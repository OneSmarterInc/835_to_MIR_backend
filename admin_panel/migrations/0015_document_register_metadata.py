from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("admin_panel", "0014_client_documents_by_client")]

    operations = [
        migrations.AddField(model_name="clientdocument", name="direction", field=models.CharField(blank=True, default="", max_length=40)),
        migrations.AddField(model_name="clientdocument", name="expiration_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="clientdocument", name="state", field=models.CharField(blank=True, default="UPLOADED", max_length=40)),
        migrations.AddField(model_name="clientdocument", name="validation_status", field=models.CharField(blank=True, default="VALID", max_length=20)),
        migrations.AddField(model_name="clientdocument", name="version", field=models.PositiveIntegerField(default=1)),
        migrations.CreateModel(
            name="ClientDocumentRegister",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(max_length=100)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="document_register", to="accounts.client")),
            ],
        ),
        migrations.AddConstraint(
            model_name="clientdocumentregister",
            constraint=models.UniqueConstraint(fields=("client", "document_type"), name="unique_client_document_register"),
        ),
    ]
