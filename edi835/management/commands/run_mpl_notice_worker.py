import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from edi835.models import MPLNotice
from edi835.mpl_notices import process_notice


PROCESSING_STATUSES = (
    "PARSING_EMAIL",
    "MATCHING_CLAIMS",
    "COLLECTING_EVIDENCE",
    "ANALYZING",
)


class Command(BaseCommand):
    help = "Process queued MPL notices with deterministic evidence and optional local Qwen."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2.0)
        parser.add_argument("--stale-minutes", type=float, default=15.0)

    def recover_stale_notices(self, stale_minutes):
        cutoff = timezone.now() - timedelta(minutes=max(stale_minutes, 1.0))
        recovered = MPLNotice.objects.filter(
            status__in=PROCESSING_STATUSES,
            updated_at__lt=cutoff,
        ).update(
            status="RECEIVED",
            last_error="Automatically requeued after an interrupted worker run.",
        )
        if recovered:
            self.stdout.write(self.style.WARNING(
                f"Requeued {recovered} stale MPL notice(s)."
            ))

    def handle(self, *args, **options):
        self.recover_stale_notices(options["stale_minutes"])
        poll_seconds = max(options["poll_seconds"], 0.2)

        while True:
            try:
                close_old_connections()
                notice_id = None
                with transaction.atomic():
                    notice = (
                        MPLNotice.objects.select_for_update(skip_locked=True)
                        .filter(status="RECEIVED")
                        .order_by("created_at")
                        .first()
                    )
                    if notice:
                        notice.status = "PARSING_EMAIL"
                        notice.save(update_fields=["status", "updated_at"])
                        notice_id = notice.id

                if notice_id:
                    processed = process_notice(notice_id)
                    self.stdout.write(
                        f"Processed MPL notice {notice_id}: {processed.status}"
                    )
                elif options["once"]:
                    return
                else:
                    time.sleep(poll_seconds)
            except Exception as exc:
                close_old_connections()
                self.stderr.write(self.style.ERROR(
                    f"MPL worker loop error: {type(exc).__name__}: {exc}"
                ))
                if options["once"]:
                    raise
                time.sleep(max(poll_seconds, 5.0))
