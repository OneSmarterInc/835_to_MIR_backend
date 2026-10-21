"""Run only the committed synthetic converter baseline, without DB/network I/O."""
import hashlib
import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from admin_panel.mir_mapper_logic.converter import convert_835_to_mir


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "phase1" / "synthetic_paid.835"


def converter_demo() -> dict:
    raw = FIXTURE.read_bytes()
    # Normalize Git's platform-specific line endings in the fixture identity.
    source = raw.decode("ascii").strip()
    output, summary = convert_835_to_mir(source, process_date=date(2026, 10, 1))
    rows = output.splitlines()
    checks = {
        "one_claim": summary["claims"] == 1,
        "one_delivered_claim": summary["delivered_claims"] == 1,
        "no_held_claims": summary["held_claims"] == 0,
        "one_service": summary["services"] == 1,
        "one_637_character_row": len(rows) == 1 and len(rows[0]) == 637,
    }
    if checks["one_637_character_row"]:
        row = rows[0]
        checks.update(
            fixed_process_date=row[36:52] == "2026100120261001",
            service_charge_200=row[384:395] == "0000020000+",
            payment_150=row[428:439] == "0000015000+",
            responsibility_50=row[439:450] == "0000005000+",
        )
    return {
        "passed": all(checks.values()), "checks": checks,
        "source_sha256": hashlib.sha256(source.encode("ascii")).hexdigest(),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_bytes": len(output.encode("utf-8")),
        "scope": "Committed synthetic 835, default mappings, fixed process date; no database writes or delivery.",
        "limitations": "Not partner certification, full guide validation, deployed client mappings or capacity evidence.",
    }


class Command(BaseCommand):
    help = "Demonstrate the synthetic default-mapping 835-to-MIR baseline."
    requires_system_checks = []

    def handle(self, *args, **options):
        report = converter_demo()
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        if not report["passed"]:
            raise CommandError("The synthetic converter baseline failed.")
