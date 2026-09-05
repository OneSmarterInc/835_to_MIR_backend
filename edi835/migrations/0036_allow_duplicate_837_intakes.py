from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("edi835", "0035_repair_837_internal_claim_numbers")]

    operations = [
        migrations.RemoveConstraint(
            model_name="edi837file",
            name="uniq_client_837_hash",
        ),
    ]
