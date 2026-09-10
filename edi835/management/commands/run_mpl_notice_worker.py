import time

from django.core.management.base import BaseCommand
from django.db import transaction

from edi835.models import MPLNotice
from edi835.mpl_notices import process_notice


class Command(BaseCommand):
    help = "Process queued MPL notices with deterministic evidence and local Qwen."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        while True:
            notice_id = None
            with transaction.atomic():
                notice = MPLNotice.objects.select_for_update(skip_locked=True).filter(status="RECEIVED").order_by("created_at").first()
                if notice:
                    notice.status = "PARSING_EMAIL"
                    notice.save(update_fields=["status", "updated_at"])
                    notice_id = notice.id
            if notice_id:
                process_notice(notice_id)
                self.stdout.write(f"Processed MPL notice {notice_id}")
            elif options["once"]:
                return
            else:
                time.sleep(max(options["poll_seconds"], 0.2))
