from django.db import migrations, models

import edi835.storage


class Migration(migrations.Migration):
    dependencies = [("admin_panel", "0013_admin_client_access_grant")]

    operations = [
        migrations.AlterField(
            model_name="clientdocument",
            name="file",
            field=models.FileField(upload_to=edi835.storage.client_document_upload_to),
        ),
    ]
