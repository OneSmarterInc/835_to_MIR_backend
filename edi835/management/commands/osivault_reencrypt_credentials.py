"""
Management command to migrate legacy Fernet and unencrypted database credentials
to OSIVault AES-256-GCM envelope encryption at rest.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from accounts.models import User
from admin_panel.models import ClientSmtpConfig
from edi835.models import SFTPConfig
from project835.field_crypto import (
    encrypt_sftp_field,
    decrypt_sftp_field,
    encrypt_smtp_password,
    decrypt_smtp_password,
)


class Command(BaseCommand):
    help = "Re-encrypt legacy credentials in database with OSIVault AES-256-GCM envelope encryption."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Scan and report records needing re-encryption without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        prefix = "[DRY-RUN] " if dry_run else ""

        self.stdout.write(self.style.NOTICE(f"{prefix}Starting OSIVault Field Credential Re-encryption..."))

        sftp_updated = 0
        smtp_updated = 0
        user_updated = 0

        with transaction.atomic():
            # 1. SFTP Configurations
            for config in SFTPConfig.objects.all():
                modified = False
                for field_name in ("password", "ssh_key", "outbound_password", "outbound_ssh_key"):
                    val = getattr(config, field_name, None)
                    if val and not val.startswith("OSV1$AES-256-GCM$"):
                        try:
                            plain = decrypt_sftp_field(val)
                            new_envelope = encrypt_sftp_field(plain)
                            setattr(config, field_name, new_envelope)
                            modified = True
                        except Exception as exc:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Skipping un-decryptable SFTP {field_name} on config {config.id}: {str(exc)}"
                                )
                            )
                if modified:
                    if not dry_run:
                        config.save()
                    sftp_updated += 1

            # 2. SMTP Configurations
            for smtp in ClientSmtpConfig.objects.all():
                val = smtp.smtp_password
                if val and not val.startswith("OSV1$AES-256-GCM$"):
                    try:
                        plain = decrypt_smtp_password(val)
                        new_envelope = encrypt_smtp_password(plain)
                        smtp.smtp_password = new_envelope
                        if not dry_run:
                            smtp.save()
                        smtp_updated += 1
                    except Exception as exc:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping un-decryptable SMTP password on config {smtp.id}: {str(exc)}"
                            )
                        )


            # 3. User TOTP Secrets
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, totp_secret FROM accounts_user WHERE totp_secret IS NOT NULL AND totp_secret != ''")
                user_rows = cursor.fetchall()

            for user_id, raw_totp in user_rows:
                if raw_totp and not raw_totp.startswith("OSV1$AES-256-GCM$"):
                    try:
                        user = User.objects.get(id=user_id)
                        # Re-saving triggers EncryptedCharField.get_prep_value encryption
                        if not dry_run:
                            user.save(update_fields=["totp_secret"])
                        user_updated += 1
                    except Exception as exc:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping TOTP secret re-encryption for user {user_id}: {str(exc)}"
                            )
                        )

            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Re-encryption completed successfully:\n"
            f"  - SFTP Configurations upgraded: {sftp_updated}\n"
            f"  - SMTP Configurations upgraded: {smtp_updated}\n"
            f"  - User TOTP Secrets upgraded:   {user_updated}"
        ))
