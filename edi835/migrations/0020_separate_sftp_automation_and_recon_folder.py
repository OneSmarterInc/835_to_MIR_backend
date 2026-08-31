from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("edi835", "0019_sftp_automation")]

    operations = [
        migrations.AddField(
            model_name="sftpconfig",
            name="inbound_recon_folder",
            field=models.CharField(blank=True, default="", max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name="sftpautomationschedule",
            name="client",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sftp_automation_schedules", to="accounts.client"),
        ),
        migrations.AddField(
            model_name="sftpautomationschedule",
            name="automation_type",
            field=models.CharField(choices=[("835", "835 to MIR"), ("837", "837 Reference"), ("RECON", "RECON")], default="835", max_length=10),
        ),
        migrations.AddField(
            model_name="sftpautomationrun",
            name="automation_type",
            field=models.CharField(choices=[("835", "835 to MIR"), ("837", "837 Reference"), ("RECON", "RECON")], default="835", max_length=10),
        ),
        migrations.AddConstraint(
            model_name="sftpautomationschedule",
            constraint=models.UniqueConstraint(fields=("client", "automation_type"), name="uniq_sftp_auto_client_type"),
        ),
        migrations.AlterModelOptions(
            name="sftpautomationschedule",
            options={"ordering": ["client__name", "automation_type"]},
        ),
    ]
