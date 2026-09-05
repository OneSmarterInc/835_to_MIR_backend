from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("edi835", "0036_allow_duplicate_837_intakes")]

    operations = [
        migrations.RemoveConstraint(
            model_name="reconfile",
            name="uniq_client_recon_hash",
        ),
    ]
