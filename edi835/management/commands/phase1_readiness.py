import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from edi835.phase1_acceptance import acceptance_gaps


class Command(BaseCommand):
    help = "Check the recorded Phase 1 approvals and evidence; no runtime changes."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--record", type=Path)
        parser.add_argument("--require-approved", action="store_true")

    def handle(self, *args, **options):
        path = options["record"] or Path(settings.BASE_DIR) / "docs" / "phase1_acceptance.json"
        try:
            with path.open("rb") as handle:
                raw = handle.read(262145)
            if len(raw) > 262144:
                raise ValueError("Acceptance record exceeds 256 KiB.")
            gaps = acceptance_gaps(json.loads(raw))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise CommandError("Invalid or unreadable Phase 1 acceptance record.") from exc
        self.stdout.write(json.dumps({
            "recorded_gate_complete": not gaps, "gaps": gaps,
            "note": "Checks recorded metadata only. Review referenced evidence and approvals; this does not certify production readiness.",
        }, indent=2))
        if options["require_approved"] and gaps:
            raise CommandError("Phase 1 evidence or approval is incomplete.")
