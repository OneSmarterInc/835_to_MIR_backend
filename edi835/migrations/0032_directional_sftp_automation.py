from django.db import migrations, models


def classify_existing_schedules(apps, schema_editor):
    Schedule = apps.get_model("edi835", "SFTPAutomationSchedule")
    Run = apps.get_model("edi835", "SFTPAutomationRun")
    Schedule.objects.filter(automation_type="835").update(direction="PROCESSING")
    Run.objects.filter(automation_type="835").update(direction="PROCESSING")


class Migration(migrations.Migration):
    dependencies = [("edi835", "0031_repair_837_claim_context")]
    operations = [
        migrations.RemoveConstraint(
            model_name="sftpautomationschedule",
            name="uniq_sftp_auto_client_type",
        ),
        migrations.AddField(
            model_name="sftpautomationschedule", name="direction",
            field=models.CharField(choices=[("INCOMING", "Incoming"), ("PROCESSING", "Processing"), ("OUTGOING", "Outgoing")], default="INCOMING", max_length=12),
        ),
        migrations.AddField(
            model_name="sftpautomationrun", name="direction",
            field=models.CharField(choices=[("INCOMING", "Incoming"), ("PROCESSING", "Processing"), ("OUTGOING", "Outgoing")], default="INCOMING", max_length=12),
        ),
        migrations.AddField(
            model_name="sftpautomationrun", name="sent_files",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="sftpautomationschedule", name="automation_type",
            field=models.CharField(choices=[("837", "837 Reference"), ("835", "835 to MIR"), ("MIR", "MIR"), ("RECON", "RECON")], default="835", max_length=10),
        ),
        migrations.AlterField(
            model_name="sftpautomationrun", name="automation_type",
            field=models.CharField(choices=[("837", "837 Reference"), ("835", "835 to MIR"), ("MIR", "MIR"), ("RECON", "RECON")], default="835", max_length=10),
        ),
        migrations.RunPython(classify_existing_schedules, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="sftpautomationschedule",
            constraint=models.UniqueConstraint(fields=("client", "automation_type", "direction"), name="uniq_sftp_auto_client_type_dir"),
        ),
    ]
