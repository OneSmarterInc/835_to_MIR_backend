from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("edi835", "0028_sftp_route_configuration")]

    operations = [
        migrations.AlterField(
            model_name="sftpconfig",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("DEFAULT", "Default SFTP"),
                    ("837_IN", "837 Inbound"),
                    ("837_OUT", "837 Outbound"),
                    ("835_IN", "835 Inbound"),
                    ("MIR_OUT", "MIR Outbound"),
                    ("RECON_IN", "RECON Inbound"),
                ],
                db_index=True,
                default="DEFAULT",
                max_length=20,
            ),
        ),
    ]
