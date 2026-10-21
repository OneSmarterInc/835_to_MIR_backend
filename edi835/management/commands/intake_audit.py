"""Read-only receipt/dispatch/storage reconciliation; no source data in output."""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Min
from django.utils import timezone

from edi835.intake_storage import source_path, storage_root
from edi835.models import BatchDispatch, InboundTransfer, IntakeOutput


class Command(BaseCommand):
    help = "Report queue age, missing sources, old orphan objects and stalled inline processing. Never deletes or retries."

    def add_arguments(self, parser):
        parser.add_argument("--grace-hours", type=int, default=24)

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(hours=max(1, options["grace_hours"]))
        missing, known = [], set()
        for receipt in InboundTransfer.objects.only("id", "storage_key").iterator(chunk_size=500):
            known.add(receipt.storage_key)
            if not source_path(receipt.storage_key).is_file():
                missing.append(str(receipt.pk))
        root = storage_root()
        known.update(IntakeOutput.objects.values_list("storage_key", flat=True))
        orphans = sum(1 for path in root.glob("*/*") if path.is_file() and
                      path.relative_to(root).as_posix() not in known and path.stat().st_mtime < cutoff.timestamp())
        oldest = BatchDispatch.objects.filter(state="QUEUED").aggregate(value=Min("created_at"))["value"]
        report = {"observed_at": now.isoformat(), "missing_source_receipt_ids": missing,
                  "unreferenced_objects_older_than_grace": orphans,
                  "queue": list(BatchDispatch.objects.values("state").annotate(count=Count("id"))),
                  "oldest_queued_seconds": (now - oldest).total_seconds() if oldest else None,
                  "stalled_inline_count": BatchDispatch.objects.filter(state="RUNNING", updated_at__lt=cutoff,
                      payload__job_type="INLINE_INTAKE").count()}
        self.stdout.write(json.dumps(report, indent=2))
