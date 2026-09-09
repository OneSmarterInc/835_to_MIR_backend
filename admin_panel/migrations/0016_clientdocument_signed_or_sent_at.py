from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("admin_panel", "0015_document_register_metadata")]

    operations = [
        migrations.AddField(
            model_name="clientdocument",
            name="signed_or_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
