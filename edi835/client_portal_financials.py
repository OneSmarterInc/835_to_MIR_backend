"""Independent, tenant-scoped financial streams for the Client User dashboard."""
from decimal import Decimal

from django.db.models import Count, Sum

from accounts.models import Client
from .client_portal_period import apply_period
from .models import EDI835Claim, MIRServiceLine, RECONClaim


def financial_metrics(client: Client, period: dict) -> dict:
    """Aggregate each stored grain once, with its own file event timestamp.

    Do not join the three streams by claim number: repeated claim occurrences
    are valid and such joins multiply amounts. MIR continuation chunks contain
    distinct services; RECON claim amounts already roll up their service rows.
    """
    received = apply_period(
        EDI835Claim.objects.filter(edi_file__client=client).exclude(
            edi_file__ingestion_source="HELD_RELEASE"
        ),
        "edi_file__uploaded_at", period,
    ).aggregate(
        billed=Sum("total_charge_amount"), paid=Sum("paid_amount"),
        patient=Sum("patient_responsibility"), count=Count("pk"),
    )
    generated = apply_period(
        MIRServiceLine.objects.filter(
            mir_claim__mir_file__client=client,
            mir_claim__mir_file__source_835__client=client,
        ),
        "mir_claim__mir_file__converted_at", period,
    ).aggregate(billed=Sum("charge_amount"), count=Count("pk"))
    reconciled = apply_period(
        RECONClaim.objects.filter(
            client=client, recon_file__client=client,
            recon_file__file_kind="RECON",
            recon_file__status__in=("PROCESSED", "PARTIAL"),
        ),
        "recon_file__uploaded_at", period,
    ).aggregate(paid=Sum("paid_amount"), count=Count("pk"))

    def money(value: Decimal | None) -> str:
        return format(value if value is not None else Decimal("0"), ".2f")

    return {
        "billed_835_amount": money(received["billed"]),
        "billed_mir_amount": money(generated["billed"]),
        # recon_service maps amountinrecon/reconamount to paid_amount;
        # reconciliation_service also uses RECONClaim.paid_amount. This is
        # neither RECON charges nor a calculated MIR waterfall/net balance.
        "recon_amount": money(reconciled["paid"]),
        "billed_835_claims": received["count"],
        "billed_mir_services": generated["count"],
        "recon_claims": reconciled["count"],
        # Preserve existing API consumers while the dashboard uses the three
        # explicit stream fields above.
        "billed_amount": money(received["billed"]),
        "paid_amount": money(received["paid"]),
        "patient_amount": money(received["patient"]),
        "normalized_claims": received["count"],
    }
