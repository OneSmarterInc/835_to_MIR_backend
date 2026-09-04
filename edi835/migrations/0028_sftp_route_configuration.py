from django.db import migrations, models


def map_existing_configs(apps, schema_editor):
    SFTPConfig = apps.get_model("edi835", "SFTPConfig")
    # Preserve every existing record. The newest record for a client/type is
    # mapped first; older duplicates stay DEFAULT without being deleted.
    for config in SFTPConfig.objects.order_by("client_id", "-updated_at"):
        if config.connection_type == "OUTBOUND":
            config.purpose = "MIR_OUT"
            config.remote_folder = config.outbound_mir_folder or ""
        else:
            config.purpose = "DEFAULT"
            config.route_paths = {
                "837_IN": config.inbound_837_folder or "",
                "835_IN": config.inbound_835_folder or "",
                "MIR_OUT": config.outbound_mir_folder or "",
                "RECON_IN": config.inbound_recon_folder or "",
            }
        config.save(update_fields=["purpose", "remote_folder", "route_paths"])


class Migration(migrations.Migration):
    dependencies = [("edi835", "0027_mir_client_filename_index")]

    operations = [
        migrations.AddField(
            model_name="sftpconfig", name="purpose",
            field=models.CharField(choices=[("DEFAULT", "Default SFTP"), ("837_IN", "837 Inbound"), ("837_OUT", "837 Outbound"), ("835_IN", "835 Inbound"), ("835_OUT", "835 Outbound"), ("MIR_OUT", "MIR Outbound"), ("RECON_IN", "RECON Inbound")], db_index=True, default="DEFAULT", max_length=20),
        ),
        migrations.AddField(
            model_name="sftpconfig", name="remote_folder",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="sftpconfig", name="setup_all_paths",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sftpconfig", name="route_paths",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(map_existing_configs, migrations.RunPython.noop),
    ]
