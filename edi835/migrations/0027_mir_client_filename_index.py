from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0026_recon_storage_fields")]

    operations = [
        migrations.AddIndex(
            model_name="mirfile",
            index=models.Index(
                fields=["client", "mir_filename"],
                name="mir_client_filename_idx",
            ),
        ),
    ]
