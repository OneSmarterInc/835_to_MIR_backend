from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from edi835.models import MIRClaim, MIRServiceLine


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
        claims = []
        for claim in MIRClaim.objects.all().iterator(chunk_size=2000):
            raw = claim.header_raw or ""
            if len(raw) >= 25:
                claim.claim_control_number = raw[2:25].strip()
                claims.append(claim)
        if claims:
            MIRClaim.objects.bulk_update(claims, ["claim_control_number"], batch_size=2000)

        pending = []
        updated = 0
        for service in MIRServiceLine.objects.all().iterator(chunk_size=2000):
            raw = service.service_raw or ""
            if len(raw) < 116:
                continue
            service.charge_amount = signed_amount(raw[50:61])
            service.allowed_amount = signed_amount(raw[83:94])
            # service_raw begins at MIR position 335, therefore the absolute
            # position formula 429 + ((N-1) * 303) becomes 95-105 here.
            service.paid_amount = signed_amount(raw[94:105])
            service.patient_liability = signed_amount(raw[105:116])
            pending.append(service)
            if len(pending) >= 2000:
                MIRServiceLine.objects.bulk_update(
                    pending,
                    ["charge_amount", "allowed_amount", "paid_amount", "patient_liability"],
                    batch_size=2000,
                )
                updated += len(pending)
                pending = []
        if pending:
            MIRServiceLine.objects.bulk_update(
                pending,
                ["charge_amount", "allowed_amount", "paid_amount", "patient_liability"],
                batch_size=2000,
            )
            updated += len(pending)
        self.stdout.write(self.style.SUCCESS(
            f"Repaired {len(claims)} MIR claim identifiers and {updated} MIR service rows."
        ))
