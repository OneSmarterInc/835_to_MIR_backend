"""Provision or revoke an intake-only machine credential from an operator shell."""
import hashlib
import secrets
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import Client
from edi835.models import IntakePartner


class Command(BaseCommand):
    help = "Issue a client-scoped partner token (printed once), or revoke a token ID."

    def add_arguments(self, parser):
        parser.add_argument("--client-code")
        parser.add_argument("--name")
        parser.add_argument("--workflow", choices=["835", "837"], action="append")
        parser.add_argument("--expires-days", type=int, default=30)
        parser.add_argument("--revoke")

    def handle(self, *args, **options):
        if options["revoke"]:
            try:
                count = IntakePartner.objects.filter(pk=options["revoke"]).update(active=False)
            except (ValueError, TypeError, ValidationError) as exc:
                raise CommandError("Invalid credential ID.") from exc
            if not count:
                raise CommandError("Credential not found.")
            self.stdout.write("Credential revoked.")
            return
        if not options["name"] or not options["workflow"] or not 1 <= options["expires_days"] <= 90:
            raise CommandError("Supply --name, --workflow and an expiry of 1–90 days.")
        client = Client.objects.filter(client_code=options["client_code"], status="ACTIVE").exclude(stage="offboarded").first()
        if not client:
            raise CommandError("Active client not found.")
        secret = secrets.token_urlsafe(32)
        credential = IntakePartner.objects.create(client=client, name=options["name"][:100],
            token_digest=hashlib.sha256(secret.encode()).hexdigest(),
            workflows=sorted(set(options["workflow"])),
            expires_at=timezone.now() + timedelta(days=options["expires_days"]))
        self.stderr.write("Store this secret securely. It is shown once; do not paste it into logs or tickets.")
        self.stdout.write(f"{credential.pk}.{secret}")
