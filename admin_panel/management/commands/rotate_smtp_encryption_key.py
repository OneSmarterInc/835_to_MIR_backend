import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from admin_panel.models import ClientSmtpConfig


class Command(BaseCommand):
    help = "Re-encrypt saved SMTP passwords from the old key to the configured new key."

    def handle(self, *args, **options):
        old_value = os.getenv("OLD_SMTP_FIELD_ENCRYPTION_KEY", "").strip()
        if not old_value:
            raise CommandError("OLD_SMTP_FIELD_ENCRYPTION_KEY is required for rotation.")
        if old_value == settings.SMTP_FIELD_ENCRYPTION_KEY:
            raise CommandError("The old and new SMTP encryption keys must differ.")
        try:
            old = Fernet(old_value.encode("utf-8"))
            new = Fernet(settings.SMTP_FIELD_ENCRYPTION_KEY.encode("utf-8"))
        except ValueError as exc:
            raise CommandError("Both encryption keys must be valid Fernet keys.") from exc

        changed = 0
        with transaction.atomic():
            for config in ClientSmtpConfig.objects.exclude(smtp_password="").select_for_update():
                try:
                    plaintext = old.decrypt(config.smtp_password.encode("utf-8"))
                except InvalidToken as exc:
                    raise CommandError(f"SMTP configuration {config.pk} cannot be decrypted with the old key.") from exc
                config.smtp_password = new.encrypt(plaintext).decode("utf-8")
                config.save(update_fields=["smtp_password"])
                changed += 1
        self.stdout.write(self.style.SUCCESS(f"Rotated {changed} SMTP credential record(s)."))
