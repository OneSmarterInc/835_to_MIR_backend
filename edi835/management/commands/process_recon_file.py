from django.core.management.base import BaseCommand, CommandError

from edi835.models import RECONFile
from edi835.recon_service import process_recon_file


class Command(BaseCommand):
    help = "Process one uploaded RECON file outside the web request."

    def add_arguments(self, parser):
        parser.add_argument("file_id")

    def handle(self, *args, **options):
        try:
            recon = RECONFile.objects.get(id=options["file_id"])
        except (RECONFile.DoesNotExist, ValueError) as exc:
            raise CommandError("RECON file was not found.") from exc
        process_recon_file(recon)
        self.stdout.write(self.style.SUCCESS(f"Processed RECON file {recon.id}"))
