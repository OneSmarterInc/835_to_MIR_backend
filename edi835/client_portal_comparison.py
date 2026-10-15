"""Tenant-isolated, period-filtered claim comparison of 835, MIR and Recon.

Accounting comparisons deliberately use like-for-like measures:
835 billed vs MIR service billed, MIR service paid vs Recon paid.
Absence of a stream is NOT a zero-dollar record. Missing rows are mismatches
with unknown financial variance until the missing stream is available.
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum, Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from project835.decorators import authenticated_api_required
from .client_portal_period import InvalidPeriod, resolve_period, period_payload, apply_period
from .client_portal_views import only_client_user
from .models import EDI835Claim, MIRClaim, RECONClaim
from .reconciliation_service import normalize_claim_id


ZERO = Decimal("0.00")
SORTABLE = {
    "claim", "date", "billed_835", "billed_mir", "paid_mir",
    "recon_amount", "mismatch_amount", "status",
}


def compare_row(key):
    return {
        "key": key, "claim": key, "claim_id": None, "file_id": None,
        "date": None, "_dt": None,
        "billed_835": None, "billed_mir": None, "paid_mir": None,
        "recon_amount": None, "mismatch_amount": None,
        "status": "Mismatch", "reason": "",
        "_835": False, "_mir": False, "_recon": False,
        "_mir_unpriced": False,
        "_835_occ": 0, "_mir_occ": 0, "_recon_occ": 0,
    }


def amount(value):
    return value if value is not None else ZERO


def add_value(row, field, value):
    row[field] = amount(row[field]) + amount(value)


def classify(row):
    missing = []
    if not row["_835"]:
        missing.append("835")
    if not row["_mir"]:
        missing.append("MIR service amounts" if row["_mir_unpriced"] else "MIR")
    if not row["_recon"]:
        missing.append("Recon")
    known_deltas = []
    if row["_835"] and row["_mir"]:
        known_deltas.append(abs(row["billed_835"] - row["billed_mir"]))
    if row["_mir"] and row["_recon"]:
        known_deltas.append(abs(row["paid_mir"] - row["recon_amount"]))
    row["mismatch_amount"] = sum(known_deltas, ZERO) if known_deltas else None
    reasons = []
    if missing:
        reasons.append("No " + ", ".join(missing) + " record in selected period")
    if len(known_deltas) >= 1 and known_deltas[0] > ZERO:
        if row["_835"] and row["_mir"]:
            reasons.append("835 and MIR billed differ")
        else:
            reasons.append("MIR and Recon paid differ")
    if len(known_deltas) == 2 and known_deltas[1] > ZERO:
        reasons.append("MIR and Recon paid differ")
    repeated = any(row[field] > 1 for field in ("_835_occ", "_mir_occ", "_recon_occ"))
    if repeated:
        reasons.append("Repeated claim identifier in the selected period; one-to-one match is uncertain")
    row["status"] = "Clear" if not missing and not repeated and all(v == ZERO for v in known_deltas) else "Mismatch"
    row["reason"] = "; ".join(reasons) or "Both billed and payment comparisons agree"
    return row


def period_rows(client, period):
    """Read each source independently, avoiding multiplying amounts by joins."""
    rows = {}
    aliases = defaultdict(set)

    received = apply_period(
        EDI835Claim.objects.filter(edi_file__client=client).exclude(
            edi_file__ingestion_source="HELD_RELEASE"
        ), "edi_file__uploaded_at", period,
    ).values(
        "id", "highmark_claim_number", "internal_claim_number",
        "total_charge_amount", "edi_file_id", "edi_file__uploaded_at"
    ).order_by("id")
    for claim in received.iterator(chunk_size=1500):
        highmark = normalize_claim_id(claim["highmark_claim_number"])
        internal = normalize_claim_id(claim["internal_claim_number"])
        control = (highmark + internal if internal and not highmark.endswith(internal)
                   else highmark) if highmark else "835-UNIDENTIFIED-" + str(claim["id"])
        row = rows.setdefault(control, compare_row(control))
        if not row["_835"]:
            row["claim"] = claim["highmark_claim_number"] or "Missing 835 claim number"
            row["claim_id"] = claim["id"]
            row["file_id"] = str(claim["edi_file_id"])
        row["_835"] = True
        row["_835_occ"] += 1
        add_value(row, "billed_835", claim["total_charge_amount"])
        event = claim["edi_file__uploaded_at"]
        if row["_dt"] is None or event > row["_dt"]:
            row["_dt"] = event
        if highmark:
            aliases[highmark].add(control)
            aliases[control].add(control)

    def target(control):
        if control in rows:
            return control
        hits = aliases.get(control)
        if hits and len(hits) == 1:
            return next(iter(hits))
        return control

    converted = apply_period(
        MIRClaim.objects.filter(
            mir_file__client=client, mir_file__source_835__client=client,
        ), "mir_file__converted_at", period,
    ).values(
        "id", "claim_control_number", "mir_file__converted_at"
    ).annotate(
        billed=Sum("service_lines__charge_amount"),
        paid=Sum("service_lines__paid_amount"),
        service_lines=Count("service_lines__id"),
    ).order_by("id")
    mir_aliases = defaultdict(set)
    for claim in converted.iterator(chunk_size=1000):
        control = normalize_claim_id(claim["claim_control_number"])
        if not control:
            control = "MIR-UNIDENTIFIED-" + str(claim["id"])
        group = target(control)
        row = rows.setdefault(group, compare_row(group))
        row["_mir_occ"] += 1
        if claim["service_lines"]:
            row["_mir"] = True
            add_value(row, "billed_mir", claim["billed"])
            add_value(row, "paid_mir", claim["paid"])
        else:
            row["_mir_unpriced"] = True
        if not row["_835"]:
            row["claim"] = claim["claim_control_number"] or "Missing MIR claim number"
        event = claim["mir_file__converted_at"]
        if row["_dt"] is None or event > row["_dt"]:
            row["_dt"] = event
        mir_aliases[control].add(group)

    reconciled = apply_period(
        RECONClaim.objects.filter(
            client=client, recon_file__client=client,
            recon_file__file_kind="RECON",
            recon_file__status__in=("PROCESSED", "PARTIAL"),
        ), "recon_file__uploaded_at", period,
    ).values(
        "id", "claim_control_number", "paid_amount", "recon_file__uploaded_at"
    ).order_by("pk")
    for claim in reconciled.iterator(chunk_size=1500):
        control = normalize_claim_id(claim["claim_control_number"])
        if not control:
            control = "RECON-UNIDENTIFIED-" + str(claim["id"])
        mir_hits = mir_aliases.get(control)
        if mir_hits and len(mir_hits) == 1:
            group = next(iter(mir_hits))
        else:
            group = target(control)
        row = rows.setdefault(group, compare_row(group))
        row["_recon"] = True
        row["_recon_occ"] += 1
        add_value(row, "recon_amount", claim["paid_amount"])
        if not row["_835"] and not row["_mir"]:
            row["claim"] = claim["claim_control_number"] or "Missing Recon claim number"
        event = claim["recon_file__uploaded_at"]
        if row["_dt"] is None or event > row["_dt"]:
            row["_dt"] = event
    return [classify(row) for row in rows.values()]


def sorted_rows(rows, sort, direction):
    descending = direction == "desc"
    def value(row):
        if sort == "claim":
            return (row["claim"] or "").casefold()
        if sort == "date":
            return row["_dt"]
        if sort == "status":
            return row["status"].casefold()
        return row[sort]
    # Unknown amounts are last in both ascending and descending views.
    known = [row for row in rows if value(row) is not None]
    missing = [row for row in rows if value(row) is None]
    known.sort(key=lambda row: (value(row), row["key"]), reverse=descending)
    missing.sort(key=lambda row: row["key"])
    return known + missing


@authenticated_api_required
@require_GET
def comparison(request):
    client, denied = only_client_user(request)
    if denied:
        return denied
    try:
        period = resolve_period(request.GET, client)
        page_size = min(100, max(1, int(request.GET.get("page_size", "10"))))
        page_no = max(1, int(request.GET.get("page", "1")))
    except (InvalidPeriod, TypeError, ValueError) as exc:
        return JsonResponse({"success": False, "error": str(exc) or "Invalid filter"}, status=400)
    sort = str(request.GET.get("sort") or "date")
    direction = str(request.GET.get("direction") or "desc")
    if sort not in SORTABLE or direction not in {"asc", "desc"}:
        return JsonResponse({"success": False, "error": "Invalid sort column or direction"}, status=400)

    rows = sorted_rows(period_rows(client, period), sort, direction)
    clear = sum(row["status"] == "Clear" for row in rows)
    mismatches = len(rows) - clear
    missing_streams = sum(
        not all((row["_835"], row["_mir"], row["_recon"])) for row in rows
    )
    confirmed = sum((row["mismatch_amount"] or ZERO for row in rows), ZERO)
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    current = min(page_no, page_count)
    response_rows = []
    for row in rows[(current - 1) * page_size:current * page_size]:
        response_rows.append({
            "key": row["key"], "claim": row["claim"],
            "claim_id": row["claim_id"], "file_id": row["file_id"],
            "date": timezone.localtime(row["_dt"]).isoformat() if row["_dt"] else None,
            **{field: format(row[field], ".2f") if row[field] is not None else None
               for field in ("billed_835", "billed_mir", "paid_mir", "recon_amount", "mismatch_amount")},
            "status": row["status"], "reason": row["reason"],
        })
    return JsonResponse({
        "success": True, "period": period_payload(period),
        "page": current, "pages": page_count, "page_size": page_size,
        "count": len(rows), "sort": sort, "direction": direction,
        "summary": {
            "clear": clear, "mismatch": mismatches,
            "missing_streams": missing_streams,
            "confirmed_mismatch_amount": format(confirmed, ".2f"),
        },
        "rows": response_rows,
    })
