from django.core.management.base import BaseCommand
from edi835.batch_jobs import import_legacy_jobs


class Command(BaseCommand):
    help = "Adopt existing JSON jobs into the database without deleting or overwriting anything. Run with web and worker stopped."

    def handle(self, *args, **options):
        self.stdout.write(f"Imported {import_legacy_jobs()} legacy jobs.")
