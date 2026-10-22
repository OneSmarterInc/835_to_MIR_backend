"""Hold-aware cleanup of private intake copies, with dry run as the default."""
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from accounts.models import Client
from edi835.intake_control import eligible_artifacts, apply_retention


class Command(BaseCommand):
    help = "Preview or apply approved private intake retention. Never deletes audit, backups or legacy archives."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Client UUID")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("Limit must be 1–1000.")
        try:
            client = Client.objects.get(pk=options["client"])
        except (Client.DoesNotExist, ValueError, ValidationError):
            raise CommandError("Client not found.")
        count = (apply_retention(client.pk, limit=options["limit"]) if options["apply"]
                 else len(eligible_artifacts(client, options["limit"])))
        self.stdout.write(f"{'Purged' if options['apply'] else 'Eligible (dry run)'}: {count}")
