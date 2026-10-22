"""Read-only source ownership report; no filenames, credentials or claim data."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.utils import timezone

from edi835.models import SFTPSourceClaim


class Command(BaseCommand):
    help = "Inspect SFTP ownership and retained sources. Never deletes, releases or replays."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("Limit must be between 1 and 1000.")
        now = timezone.now()
        review = SFTPSourceClaim.objects.filter(
            Q(state="REVIEW") | Q(state="STORED", lease_expires_at__lte=now)
        ).order_by("updated_at")
        self.stdout.write(json.dumps({
            "states": list(SFTPSourceClaim.objects.values("state").annotate(count=Count("pk"))),
            "active_leases": SFTPSourceClaim.objects.filter(lease_expires_at__gt=now).count(),
            "review_count": review.count(),
            "review": [{"source_identity": row.pk, "receipt_id": str(row.receipt_id or ""),
                        "client_id": str(row.client_id or ""), "state": row.state}
                       for row in review[:options["limit"]]],
        }, indent=2))
