"""Tenant-isolated, period-filtered claim comparison of 835, MIR and Recon.

Accounting comparisons deliberately use like-for-like measures:
835 billed vs MIR service billed, 835 paid vs MIR service paid, and,
separately, MIR service paid vs Recon paid.
Absence of a stream is NOT a zero-dollar record. Missing rows are mismatches
with unknown financial variance until the missing stream is available.
"""
from collections import defaultdict
from types import SimpleNamespace
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
from .models import EDI835Claim, EDI835File, MIRClaim, RECONClaim, ClientPortalAction
from .reconciliation_service import normalize_claim_id


ZERO = Decimal("0.00")
SORTABLE = {
    "claim", "date", "billed_835", "billed_mir", "paid_835", "paid_mir",
    "recon_amount", "mismatch_amount", "status",
    "waiting_days", "internal_number", "payer_status", "hold_reason", "action_status", "latest_note",
    "issue_label", "latest_note_action",
}


def compare_row(key):
    return {
        "key": key, "claim": key, "claim_id": None, "file_id": None,
        "date": None, "_dt": None,
        "id": None, "waiting_days": None,
        "waiting_basis": None, "is_held": False,
        "internal_number": "", "hold_reason": "", "action_status": "",
        "issue_label": "", "issue_detail": "", "issue_tone": "neutral", "latest_note_action": "",
        "latest_note": "", "latest_note_at": None, "review_activity": [],
        "billed_835": None, "billed_mir": None, "paid_835": None, "paid_mir": None,
        "recon_amount": None, "mismatch_amount": None,
        "status": "Mismatch", "reason": "",
        "_835": False, "_mir": False, "_recon": False,
        "_mir_unpriced": False,
        "_835_occ": 0, "_mir_occ": 0, "_recon_occ": 0,
        "_mir_services": 0, "_835_denied": False,
        "payer_status": None,
    }


def amount(value):
    return value if value is not None else ZERO


def add_value(row, field, value):
    row[field] = amount(row[field]) + amount(value)


def source_ready(row, source):
    """Only complete, unique records support a confirmed comparison."""
    return (row["_" + source] and row["_" + source + "_occ"] == 1
            and not (source == "mir" and row["_mir_unpriced"]))


COMPARISONS = (
    ("835", "mir", "billed_835", "billed_mir", "835 and MIR billed differ"),
    ("835", "mir", "paid_835", "paid_mir", "835 and MIR paid differ"),
    ("mir", "recon", "paid_mir", "recon_amount", "MIR and Recon paid differ"),
)


def classify(row):
    missing = []
    if not row["_835"]:
        missing.append("835")
    if not row["_mir"] or row["_mir_unpriced"]:
        missing.append("MIR service amounts" if row["_mir_unpriced"] else "MIR")
    if not row["_recon"]:
        missing.append("Recon")
    reasons = []
    if missing:
        reasons.append("No " + ", ".join(missing) + " record in selected period")
    known_deltas = []
    for left_source, right_source, left, right, reason in COMPARISONS:
        if source_ready(row, left_source) and source_ready(row, right_source):
            delta = abs(row[left] - row[right])
            known_deltas.append(delta)
            if delta > ZERO:
                reasons.append(reason)
    row["mismatch_amount"] = sum(known_deltas, ZERO) if known_deltas else None
    repeated = any(row[field] > 1 for field in ("_835_occ", "_mir_occ", "_recon_occ"))
    if repeated:
        reasons.append("Repeated claim identifier in the selected period; one-to-one match is uncertain")
    row["status"] = "Clear" if not missing and not repeated and all(v == ZERO for v in known_deltas) else "Mismatch"
    row["reason"] = "; ".join(reasons) or "835/MIR billed and paid, and MIR/Recon paid agree"
    if row["_835"]:
        if row["_835_denied"]:
            row["payer_status"] = "Denied"
        elif row["paid_835"] >= row["billed_835"]:
            row["payer_status"] = "Paid in full"
        elif row["paid_835"] > ZERO:
            row["payer_status"] = "Paid less than billed"
        else:
            row["payer_status"] = "No payment"
    return row


def summarize_issue(row):
    """Keep 835/MIR status independent of Recon and conversion holds."""
    reasons = []
    for source, label in (("835", "835"), ("mir", "MIR")):
        if row["_" + source + "_occ"] > 1:
            reasons.append("Duplicate " + label)
        if not row["_" + source + "_occ"]:
            reasons.append(label + " record missing")
        elif source == "mir" and row["_mir_unpriced"]:
            reasons.append("MIR amounts missing")
    if source_ready(row, "835") and source_ready(row, "mir"):
        billed_differs = row["billed_835"] != row["billed_mir"]
        paid_differs = row["paid_835"] != row["paid_mir"]
        if billed_differs and paid_differs:
            reasons.append("Billed & paid differ")
        elif billed_differs:
            reasons.append("Billed differs")
        elif paid_differs:
            reasons.append("Paid differs")
    row["issue_label"] = " · ".join(reasons[:2]) or "Billed & paid match"
    if len(reasons) > 2:
        row["issue_label"] += f" +{len(reasons) - 2}"
    row["issue_tone"] = "error" if reasons else "clear"

    def money(field):
        return format(row[field], ".2f") if row[field] is not None else "unavailable"

    details = [
        "835/MIR: " + ("; ".join(reasons) or "billed and paid match"),
        f"Billed: 835 {money('billed_835')} / MIR {money('billed_mir')}",
        f"Paid: 835 {money('paid_835')} / MIR {money('paid_mir')}",
    ]
    if any(row[name] > 1 for name in ("_835_occ", "_mir_occ")):
        details.append("Aggregate amounts shown; one-to-one match is uncertain")
    if row["hold_reason"]:
        details.append("Conversion hold: " + row["hold_reason"])
    row["issue_detail"] = " | ".join(details)

    recon_reasons = []
    for source, label in (("mir", "MIR"), ("recon", "Recon")):
        if row["_" + source + "_occ"] > 1:
            recon_reasons.append("Duplicate " + label)
        if not row["_" + source + "_occ"]:
            recon_reasons.append(label + " record missing")
        elif source == "mir" and row["_mir_unpriced"]:
            recon_reasons.append("MIR amounts missing")
    if source_ready(row, "mir") and source_ready(row, "recon"):
        if row["paid_mir"] != row["recon_amount"]:
            recon_reasons.append("Paid differs")
    row["recon_issue_label"] = " · ".join(recon_reasons[:2]) or "Paid matches"
    row["recon_issue_tone"] = "error" if recon_reasons else "clear"
    row["recon_issue_detail"] = (
        "MIR/Recon: " + ("; ".join(recon_reasons) or "paid amounts match")
        + f" | Paid: MIR {money('paid_mir')} / Recon {money('recon_amount')}"
    )
    return row


def period_rows(client, period):
    """Read each source independently, avoiding multiplying amounts by joins."""
    rows = {}
    aliases = defaultdict(set)
    file_claim_keys = defaultdict(list)
    hold_findings = {}
    for held in apply_period(
        EDI835File.objects.filter(client=client, held_claims_count__gt=0),
        "uploaded_at", period,
    ).values("id", "conversion_findings", "uploaded_at").iterator(chunk_size=300):
        hold_findings[held["id"]] = (
            [finding for finding in (held["conversion_findings"] or [])
             if isinstance(finding, dict)
             and str(finding.get("severity") or "").upper() in ("HOLD", "REFUSE")],
            held["uploaded_at"],
        )
    zone = ZoneInfo(period["timezone"])

    received = apply_period(
        EDI835Claim.objects.filter(edi_file__client=client).exclude(
            edi_file__ingestion_source="HELD_RELEASE"
        ), "edi_file__uploaded_at", period,
    ).values(
        "id", "highmark_claim_number", "internal_claim_number",
        "total_charge_amount", "paid_amount", "claim_status", "claim_sequence",
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
            row["id"] = claim["id"]
            row["file_id"] = str(claim["edi_file_id"])
            row["internal_number"] = claim["internal_claim_number"] or ""
        event = claim["edi_file__uploaded_at"]
        # The original 835 arrival date is available even when conversion has
        # no HOLD/REFUSE finding. Never show a misleading blank age simply
        # because this claim was successfully converted.
        received_days = max(0, (period["today"] - timezone.localtime(event, zone).date()).days)
        if row["waiting_days"] is None or received_days > row["waiting_days"]:
            row["waiting_days"] = received_days
            row["waiting_basis"] = "835 received"
        source_hold = hold_findings.get(claim["edi_file_id"])
        if source_hold:
            from .client_portal_views import finding_for
            match = finding_for(SimpleNamespace(
                highmark_claim_number=claim["highmark_claim_number"],
                internal_claim_number=claim["internal_claim_number"],
                claim_sequence=claim["claim_sequence"],
            ), source_hold[0])
            if match:
                row["hold_reason"] = str(
                    match.get("reason") or match.get("detail") or
                    match.get("rule_code") or "Conversion hold"
                )
                row["is_held"] = True
                # Keep age from the 835's received date; HOLD findings explain
                # why it remains held, not the beginning of a different clock.
        row["_835"] = True
        row["_835_occ"] += 1
        add_value(row, "paid_835", claim["paid_amount"])
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
        "mir_file__source_835_id", "service_count"
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
        if claim["service_count"] > claim["service_line_count"]:
            row["_mir_unpriced"] = True
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
    for row in rows.values():
        # MIR-only or Recon-only rows cannot have an 835 waiting period.
        # Display their age since the most recent available file event,
        # distinguishing that basis from an 835 claim's received age.
        if row["waiting_days"] is None and row["_dt"] is not None:
            row["waiting_days"] = max(
                0, (period["today"] - timezone.localtime(row["_dt"], zone).date()).days
            )
            row["waiting_basis"] = "latest available file activity"
    return [summarize_issue(classify(row)) for row in rows.values()]


def enrich_activity(client, rows):
    """One recent note and one action status per claim, without per-row queries.

    Chunk claim IDs to avoid SQLite's bound-parameter limit for All histories.
    """
    by_claim = {row["claim_id"]: row for row in rows if row["claim_id"] is not None}
    identifiers = list(by_claim)
    if not identifiers:
        return
    for offset in range(0, len(identifiers), 400):
        subset = identifiers[offset:offset + 400]
        qs = ClientPortalAction.objects.filter(
            client=client, claim_id__in=subset, file__client=client,
        ).values(
            "claim_id", "kind", "status", "message", "created_at",
            "created_by_label",
        ).order_by("-created_at", "-pk")
        for event in qs.iterator(chunk_size=400):
            row = by_claim.get(event["claim_id"])
            if row is None:
                continue
            if event["kind"] == "NOTE":
                if not row["latest_note"]:
                    row["latest_note"] = event["message"]
                    row["latest_note_at"] = event["created_at"].isoformat()
                continue
            if event["kind"].startswith("REQUEST_"):
                if event["status"] == "OPEN":
                    # Pending requests take precedence over later notes and
                    # reviewed requests; never hide actionable work behind a
                    # "Note added" label in the unified table.
                    row["action_status"] = "Pending review"
                elif not row["action_status"]:
                    row["action_status"] = "Reviewed"
            elif not row["action_status"]:
                row["action_status"] = "Reviewed" if event["status"] == "REVIEWED" else "Open"
    for row in rows:
        # Use the same compact label for sorting, display and full CSV export.
        action = row["action_status"]
        note = row["latest_note"]
        row["latest_note_action"] = (action + ": " + note if action and note else
                                     action or ("Note added: " + note if note else ""))


def sorted_rows(rows, sort, direction):
    descending = direction == "desc"
    def value(row):
        if sort == "claim":
            return (row["claim"] or "").casefold()
        if sort == "date":
            return row["_dt"]
        if sort == "status":
            return row["status"].casefold()
        if sort in {"internal_number", "payer_status", "hold_reason", "action_status", "latest_note", "issue_label", "latest_note_action"}:
            return (row.get(sort) or "").casefold()
        return row[sort]
    # Unknown amounts are last in both ascending and descending views.
    known = [row for row in rows if value(row) is not None]
    missing = [row for row in rows if value(row) is None]
    known.sort(key=lambda row: (value(row), row["key"]), reverse=descending)
    missing.sort(key=lambda row: row["key"])
    return known + missing


CSV_COLUMNS = (
    ("waiting_days", "Days waiting"),
    ("claim", "Claim"),
    ("internal_number", "Internal claim"),
    ("billed_835", "Billed in 835"),
    ("billed_mir", "Billed MIR"),
    ("paid_835", "Paid 835"),
    ("paid_mir", "Paid MIR"),
    ("recon_amount", "Amount in Recon"),
    ("mismatch_amount", "Mismatch amount"),
    ("issue_label", "835/MIR status"),
    ("recon_issue_label", "MIR/Recon status"),
    ("latest_note_action", "Latest note / action"),
    ("payer_status", "Payer status"),
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
                "internal_number": csv_safe_text(row["internal_number"]),
                "waiting_days": row["waiting_days"] if row["waiting_days"] is not None else "",
                "payer_status": csv_safe_text(row["payer_status"]),
                "issue_label": csv_safe_text(row["issue_label"]),
                "recon_issue_label": csv_safe_text(row["recon_issue_label"]),
                "latest_note_action": csv_safe_text(row["latest_note_action"]),
                "date": date,
            }
            for key in ("billed_835", "billed_mir", "paid_835", "paid_mir", "recon_amount", "mismatch_amount"):
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
    sort = str(request.GET.get("sort") or "waiting_days")
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
    enrich_activity(client, period_data)
    rows = sorted_rows(period_data, sort, direction)
    export_format = str(request.GET.get("format") or "json").strip().lower()
    if export_format not in {"json", "csv"}:
        return JsonResponse({"success": False, "error": "Unsupported export format"}, status=400)
    if export_format == "csv":
        return export_csv(rows, period, payer, sort, direction)

    clear = sum(row["status"] == "Clear" for row in rows)
    mismatches = len(rows) - clear
    missing_streams = sum(
        not all((row["_835"], row["_mir"], row["_recon"])) or row["_mir_unpriced"]
        for row in rows
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
            "id": row["claim_id"], "internal_number": row["internal_number"],
            "waiting_days": row["waiting_days"], "waiting_basis": row["waiting_basis"],
            "is_held": row["is_held"], "hold_reason": row["hold_reason"],
            "payer_status": row["payer_status"],
            "action_status": row["action_status"],
            "latest_note": row["latest_note"], "latest_note_at": row["latest_note_at"],
            "latest_note_action": row["latest_note_action"],
            "issue_label": row["issue_label"], "issue_detail": row["issue_detail"],
            "issue_tone": row["issue_tone"],
            "recon_issue_label": row["recon_issue_label"],
            "recon_issue_detail": row["recon_issue_detail"],
            "recon_issue_tone": row["recon_issue_tone"],
            "file_id": row["file_id"],
            "date": timezone.localtime(row["_dt"]).isoformat() if row["_dt"] else None,
            **{field: format(row[field], ".2f") if row[field] is not None else None
               for field in ("billed_835", "billed_mir", "paid_835", "paid_mir", "recon_amount", "mismatch_amount")},
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
