from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import migrations


def encrypt_existing_credentials(apps, schema_editor):
    key = getattr(settings, "SMTP_FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise RuntimeError(
            "SMTP_FIELD_ENCRYPTION_KEY must be set before running this migration."
        )

    fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypted(value):
        if not value:
            return value
        try:
            fernet.decrypt(value.encode())
            return value
        except (InvalidToken, ValueError, TypeError):
            return fernet.encrypt(value.encode()).decode()

    SFTPConfig = apps.get_model("edi835", "SFTPConfig")
    for config in SFTPConfig.objects.all().iterator():
        password = encrypted(config.password)
        outbound_password = encrypted(config.outbound_password)
        updates = []
        if password != config.password:
            config.password = password
            updates.append("password")
        if outbound_password != config.outbound_password:
            config.outbound_password = outbound_password
            updates.append("outbound_password")
        if updates:
            config.save(update_fields=updates)

    ClientSmtpConfig = apps.get_model("admin_panel", "ClientSmtpConfig")
    for config in ClientSmtpConfig.objects.all().iterator():
        smtp_password = encrypted(config.smtp_password)
        if smtp_password != config.smtp_password:
            config.smtp_password = smtp_password
            config.save(update_fields=["smtp_password"])


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0008_seed_onboarding_steps"),
        ("edi835", "0011_sftpconfig_use_default"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing_credentials, migrations.RunPython.noop),
    ]
