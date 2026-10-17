"""Backfill normalized Analysis columns and recover omitted responsibility."""
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from edi835.client_portal_analysis import parse_analysis, persist_analysis_result
from edi835.models import AnalysisUpload


class Command(BaseCommand):
    help = "Reprocess legacy Analysis sources into normalized staging tables (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save verified results.")
        parser.add_argument("--client-id", type=UUID, help="Limit processing to one client.")

    def handle(self, *args, **options):
        uploads = AnalysisUpload.objects.filter(processing_version__lt=2).order_by("pk")
        if options["client_id"] is not None:
            uploads = uploads.filter(client_id=options["client_id"])
        accepted = rejected_count = failed = 0
        for upload in uploads.iterator(chunk_size=1):
            try:
                rejected = []
                claims = parse_analysis(bytes(upload.raw_content).decode("utf-8-sig"), upload.kind,
                                        rejected=rejected, details=True)
                if options["apply"]:
                    saved = persist_analysis_result(upload, claims, rejected)
                    accepted += saved.accepted_count
                    rejected_count += saved.rejected_count
                else:
                    accepted += len(claims)
                    rejected_count += len(rejected)
            except ValueError:
                failed += 1
                self.stderr.write(f"Upload ID {upload.pk}: source failed structural validation; left unchanged.")
        action = "Processed" if options["apply"] else "Would process"
        self.stdout.write(f"{action}: {accepted} accepted claims, {rejected_count} rejected claims, {failed} failed files.")
        if not options["apply"]:
            self.stdout.write("Dry run validates source data; exact duplicate decisions are applied during saving.")
        if failed:
            raise CommandError("Some sources could not be processed. Review the listed upload IDs.")
