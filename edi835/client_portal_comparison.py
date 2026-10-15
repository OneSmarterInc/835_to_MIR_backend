"""Tenant-isolated, period-filtered claim comparison of 835, MIR and Recon.

Accounting comparisons deliberately use like-for-like measures:
835 billed vs MIR service billed, MIR service paid vs Recon paid.
Absence of a stream is NOT a zero-dollar record. Missing rows are mismatches
with unknown financial variance until the missing stream is available.
"""
from collections import defaultdict
import csv
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Sum, Count
from django.http import JsonResponse, StreamingHttpResponse
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
        "_mir_services": 0, "_835_paid": ZERO, "_835_denied": False,
        "payer_status": None,
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
    if row["_835"]:
        if row["_835_denied"]:
            row["payer_status"] = "Denied"
        elif row["_835_paid"] >= row["billed_835"]:
            row["payer_status"] = "Paid in full"
        elif row["_835_paid"] > ZERO:
            row["payer_status"] = "Paid less than billed"
        else:
            row["payer_status"] = "No payment"
    return row


def period_rows(client, period):
    """Read each source independently, avoiding multiplying amounts by joins."""
    rows = {}
    aliases = defaultdict(set)
    file_claim_keys = defaultdict(list)

    received = apply_period(
        EDI835Claim.objects.filter(edi_file__client=client).exclude(
            edi_file__ingestion_source="HELD_RELEASE"
        ), "edi_file__uploaded_at", period,
    ).values(
        "id", "highmark_claim_number", "internal_claim_number",
        "total_charge_amount", "paid_amount", "claim_status",
        "edi_file_id", "edi_file__uploaded_at"
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
        row["_835_paid"] += amount(claim["paid_amount"])
        row["_835_denied"] = row["_835_denied"] or claim["claim_status"] == "4"
        add_value(row, "billed_835", claim["total_charge_amount"])
        event = claim["edi_file__uploaded_at"]
        if row["_dt"] is None or event > row["_dt"]:
            row["_dt"] = event
        if highmark:
            aliases[highmark].add(control)
            aliases[control].add(control)
            file_claim_keys[claim["edi_file_id"]].append((highmark, control))

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
        "id", "claim_control_number", "mir_file__converted_at",
        "mir_file__source_835_id"
    ).annotate(
        billed=Sum("service_lines__charge_amount"),
        paid=Sum("service_lines__paid_amount"),
        service_line_count=Count("service_lines__id"),
    ).order_by("id")
    mir_aliases = defaultdict(set)
    for claim in converted.iterator(chunk_size=1000):
        control = normalize_claim_id(claim["claim_control_number"])
        if not control:
            control = "MIR-UNIDENTIFIED-" + str(claim["id"])
        group = target(control)
        if group not in rows:
            # The canonical MIR control sometimes extends CLP01 with a CLP07
            # suffix which 835 normalization may not have recognized. Only
            # resolve that suffix inside this MIR's linked source 835 file,
            # and only when it identifies exactly one claim.
            candidates = {
                key for external, key in file_claim_keys.get(
                    claim["mir_file__source_835_id"], []
                ) if control.startswith(external) and len(control) > len(external)
            }
            if len(candidates) == 1:
                group = next(iter(candidates))
        row = rows.setdefault(group, compare_row(group))
        row["_mir_occ"] += 1
        if claim["service_line_count"]:
            row["_mir"] = True
            row["_mir_services"] += claim["service_line_count"]
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


CSV_COLUMNS = (
    ("claim", "Claim"),
    ("date", "Latest activity"),
    ("billed_835", "Billed in 835"),
    ("billed_mir", "Billed MIR"),
    ("paid_mir", "Paid MIR"),
    ("recon_amount", "Amount in Recon"),
    ("mismatch_amount", "Mismatch amount"),
    ("status", "Status"),
    ("reason", "Mismatch reason"),
)


def csv_safe_text(value):
    """Prevent spreadsheet formula execution in exported untrusted identifiers.

    Numeric Decimal fields are handled separately, preserving genuine negative
    amounts; only human-supplied/free-text columns require formula escaping.
    """
    text = str(value or "")
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


class _CsvSink:
    def write(self, value):
        return value


def export_csv(rows, period, payer, sort, direction):
    """Stream *every* matching sorted row; no pagination truncation."""
    writer = csv.writer(_CsvSink(), lineterminator="\r\n")
    zone = ZoneInfo(period["timezone"])

    def body():
        yield "\ufeff"  # Excel-compatible UTF-8; header follows the BOM.
        yield writer.writerow([label for _, label in CSV_COLUMNS])
        for row in rows:
            date = timezone.localtime(row["_dt"], zone).strftime("%Y-%m-%d %H:%M:%S %Z") if row["_dt"] else ""
            export = {
                "claim": csv_safe_text(row["claim"]),
                "date": date,
                "status": row["status"],
                "reason": csv_safe_text(row["reason"] if row["status"] == "Mismatch" else ""),
            }
            for key in ("billed_835", "billed_mir", "paid_mir", "recon_amount", "mismatch_amount"):
                amount_value = row[key]
                export[key] = format(amount_value, ".2f") if amount_value is not None else ""
            yield writer.writerow([export.get(key, "") for key, _ in CSV_COLUMNS])

    scope = period["mode"]
    if period["from"]:
        scope += "_" + period["from"].strftime("%Y%m%d") + "-" + period["to"].strftime("%Y%m%d")
    filename = f"filex-835-mir-recon_{scope}_{payer}_{sort}-{direction}.csv"
    response = StreamingHttpResponse(body(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response


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
    payer = str(request.GET.get("payer_status") or "all").strip().lower()
    if sort not in SORTABLE or direction not in {"asc", "desc"}:
        return JsonResponse({"success": False, "error": "Invalid sort column or direction"}, status=400)
    categories = {
        "all": None, "denied": {"Denied", "No payment"},
        "less": {"Paid less than billed"}, "paid": {"Paid in full"},
    }
    if payer not in categories:
        return JsonResponse({"success": False, "error": "Invalid payer status filter"}, status=400)

    period_data = period_rows(client, period)
    if payer != "all":
        period_data = [row for row in period_data if row["payer_status"] in categories[payer]]
    rows = sorted_rows(period_data, sort, direction)
    export_format = str(request.GET.get("format") or "json").strip().lower()
    if export_format not in {"json", "csv"}:
        return JsonResponse({"success": False, "error": "Unsupported export format"}, status=400)
    if export_format == "csv":
        return export_csv(rows, period, payer, sort, direction)

    clear = sum(row["status"] == "Clear" for row in rows)
    mismatches = len(rows) - clear
    missing_streams = sum(
        not all((row["_835"], row["_mir"], row["_recon"])) for row in rows
    )
    confirmed = sum((row["mismatch_amount"] or ZERO for row in rows), ZERO)
    def stream_total(field):
        return format(sum((row[field] for row in rows if row[field] is not None), ZERO), ".2f")

    financial_totals = {
        "billed_835_amount": stream_total("billed_835"),
        "billed_mir_amount": stream_total("billed_mir"),
        "recon_amount": stream_total("recon_amount"),
        "billed_835_claims": sum(row["_835_occ"] for row in rows),
        "billed_mir_services": sum(row["_mir_services"] for row in rows),
        "recon_claims": sum(row["_recon_occ"] for row in rows),
    }
    billed_gap = abs(
        Decimal(financial_totals["billed_835_amount"])
        - Decimal(financial_totals["billed_mir_amount"])
    )
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    current = min(page_no, page_count)
    response_rows = []
    for row in rows[(current - 1) * page_size:current * page_size]:
        response_rows.append({
            "key": row["key"], "claim": row["claim"],
            "claim_id": row["claim_id"], "record_id": row["claim_id"],
            "file_id": row["file_id"],
            "date": timezone.localtime(row["_dt"]).isoformat() if row["_dt"] else None,
            **{field: format(row[field], ".2f") if row[field] is not None else None
               for field in ("billed_835", "billed_mir", "paid_mir", "recon_amount", "mismatch_amount")},
            "status": row["status"], "reason": row["reason"],
        })
    return JsonResponse({
        "success": True, "period": period_payload(period),
        "page": current, "pages": page_count, "page_size": page_size,
        "count": len(rows), "sort": sort, "direction": direction,
        "payer_status": payer, "financial_totals": financial_totals,
        "summary": {
            "clear": clear, "mismatch": mismatches,
            "missing_streams": missing_streams,
            "confirmed_mismatch_amount": format(confirmed, ".2f"),
            "billed_gap_amount": format(billed_gap, ".2f"),
        },
        "rows": response_rows,
    })
