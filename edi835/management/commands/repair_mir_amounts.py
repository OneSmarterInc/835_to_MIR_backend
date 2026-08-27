from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from edi835.models import MIRServiceLine


def signed_amount(value):
    value = (value or "").strip()
    if not value:
        return Decimal("0")
    sign = Decimal("-1") if value[-1:] == "-" else Decimal("1")
    digits = value[:-1] if value[-1:] in "+-" else value
    try:
        return sign * Decimal(digits or "0").scaleb(-2)
    except InvalidOperation:
        return Decimal("0")


class Command(BaseCommand):
    help = "Repair persisted MIR service amounts created before target-key extraction was fixed."

    def handle(self, *args, **options):
        pending = []
        updated = 0
        for service in MIRServiceLine.objects.all().iterator(chunk_size=2000):
            raw = service.service_raw or ""
            if len(raw) < 116:
                continue
            service.charge_amount = signed_amount(raw[50:61])
            service.paid_amount = signed_amount(raw[94:105])
            service.patient_liability = signed_amount(raw[105:116])
            pending.append(service)
            if len(pending) >= 2000:
                MIRServiceLine.objects.bulk_update(
                    pending, ["charge_amount", "paid_amount", "patient_liability"], batch_size=2000
                )
                updated += len(pending)
                pending = []
        if pending:
            MIRServiceLine.objects.bulk_update(
                pending, ["charge_amount", "paid_amount", "patient_liability"], batch_size=2000
            )
            updated += len(pending)
        self.stdout.write(self.style.SUCCESS(f"Repaired {updated} MIR service rows."))
