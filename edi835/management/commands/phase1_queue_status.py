import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from edi835.queue_observation import observe_queue


class Command(BaseCommand):
    help = "Inspect queue counts and age without modifying jobs or exposing their contents."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--legacy-files", action="store_true", help="Inspect only pre-migration JSON jobs.")
        parser.add_argument("--max-files", type=int, default=5000)
        parser.add_argument("--max-queue-age-seconds", type=int)
        parser.add_argument("--fail-if-unhealthy", action="store_true")
        parser.add_argument("--approved-target", action="store_true", help="Use the target approved in Intake Control.")

    def handle(self, *args, **options):
        limit = options["max_queue_age_seconds"]
        if options["approved_target"]:
            if limit is not None:
                raise CommandError("Choose an explicit threshold or --approved-target, not both.")
            from edi835.models import IntakeOperatingTargets
            limit = IntakeOperatingTargets.objects.filter(pk=1).values_list("queue_age_seconds", flat=True).first()
        if not 1 <= options["max_files"] <= 100000:
            raise CommandError("--max-files must be between 1 and 100000.")
        if limit is not None and limit < 1:
            raise CommandError("--max-queue-age-seconds must be positive.")
        if options["fail_if_unhealthy"] and limit is None:
            raise CommandError("Supply an approved --max-queue-age-seconds threshold first.")
        now = timezone.now()
        # Do not use jobs_dir(): inspection must not create missing directories.
        if options["legacy_files"]:
            report = observe_queue(Path(settings.MEDIA_ROOT) / "edi835" / "batch_jobs",
                                   now, max_files=options["max_files"])
            report["backend"] = "legacy_files"
        else:
            from django.db.models import Count, Min
            from edi835.models import BatchDispatch
            counts = dict(BatchDispatch.objects.values("state").annotate(n=Count("id")).values_list("state", "n"))
            oldest = BatchDispatch.objects.filter(state="QUEUED").aggregate(value=Min("created_at"))["value"]
            report = {"backend": "database", "scan_complete": True, "counts": counts,
                      "oldest_queued_seconds": (now - oldest).total_seconds() if oldest else None}
        oldest = report["oldest_queued_seconds"]
        if not report["scan_complete"]:
            assessment = "unknown"
        elif limit is None:
            assessment = "threshold_not_configured"
        elif oldest is not None and oldest > limit:
            assessment = "queue_age_exceeded"
        else:
            assessment = "within_queue_age_limit"
        report.update(observed_at=now.isoformat(), assessment=assessment,
                      max_queue_age_seconds=limit)
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        if options["fail_if_unhealthy"] and assessment != "within_queue_age_limit":
            raise CommandError("Queue observation is incomplete or exceeds the supplied age limit.")
