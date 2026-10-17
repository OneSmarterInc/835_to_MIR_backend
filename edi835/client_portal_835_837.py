"""Client-scoped billed-charge exceptions for an 837 arrival cohort."""
from collections import defaultdict
import csv
import json
from zoneinfo import ZoneInfo

from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from project835.decorators import authenticated_api_required
from .claim_numbers import split_claim_number
from .client_portal_comparison import AGE_RANGES, age_summary, csv_safe_text, _CsvSink
from .client_portal_period import InvalidPeriod, resolve_period, apply_period, period_payload
from .client_portal_views import only_client_user
from .models import EDI835Claim, EDI837Claim


COLUMNS = (("waiting_days", "Waiting days"), ("claim", "Claim"),
           ("internal_number", "Internal claim"), ("billed_837", "Billed 837"),
           ("billed_835", "Billed 835"), ("difference", "Billed difference"),
           ("status", "835/837 status"), ("source_837", "837 source"))
MONEY_FIELDS = {"billed_837", "billed_835", "difference"}


def identity(number: str, internal: str) -> tuple[str, str]:
    # Preserve punctuation in claim numbers; never use substring matching.
    number = (number or "").strip().upper()
    internal = (internal or "").strip().upper()
    if internal.startswith("ZZ:"):
        internal = internal[3:]
    return number, internal


def submission_identity(row: dict) -> tuple[str, str]:
    split = split_claim_number(row["claim_control_number"])
    return identity(
        row["highmark_claim_number"] or split["highmark_claim_number"] or row["claim_control_number"],
        row["internal_claim_number"] or (split["internal_claim_number"] if split["highmark_claim_number"] else ""),
    )


def exception_rows(client, period: dict) -> tuple[list[dict], int]:
    submissions = defaultdict(list)
    remittances = defaultdict(list)
    all_submission_keys = defaultdict(set)
    fields = ("id", "highmark_claim_number", "internal_claim_number", "claim_control_number",
              "edi_file__uploaded_at", "edi_file__original_filename", "total_charge_amount")
    # Resolve identity ambiguity over all history, not just the selected day.
    all_837 = EDI837Claim.objects.filter(client=client, edi_file__client=client)
    for row in apply_period(all_837, "edi_file__uploaded_at", period).values(*fields).iterator(chunk_size=1500):
        key = submission_identity(row)
        if key[0]:
            submissions[key].append(row)
    if not submissions:
        return [], 0
    selected_numbers = {key[0] for key in submissions}
    for row in all_837.values("highmark_claim_number", "internal_claim_number", "claim_control_number").iterator(chunk_size=1500):
        key = submission_identity(row)
        if key[0] in selected_numbers:
            all_submission_keys[key[0]].add(key)
    for row in EDI835Claim.objects.filter(edi_file__client=client).exclude(
        edi_file__ingestion_source="HELD_RELEASE"
    ).values("id", "highmark_claim_number", "internal_claim_number", "total_charge_amount",
             "edi_file__original_filename").iterator(chunk_size=1500):
        key = identity(row["highmark_claim_number"], row["internal_claim_number"])
        if key[0] in all_submission_keys:
            remittances[key[0]].append((key, row))
    output, ambiguous = [], 0
    zone = ZoneInfo(period["timezone"])
    for key, submitted in submissions.items():
        candidates = remittances.get(key[0], [])
        exact = [row for match, row in candidates if match == key]
        possible = [(match, row) for match, row in candidates if match == key or not match[1] or not key[1]]
        if exact:
            matches = exact
        elif possible and len(all_submission_keys[key[0]]) == 1:
            matches = [row for _, row in possible]
        elif possible:
            ambiguous += 1
            continue
        else:
            matches = []
        # Repeated claim records cannot establish a one-to-one billed comparison.
        if (matches and len(submitted) != 1) or len(matches) > 1:
            ambiguous += 1
            continue
        for claim in submitted:
            received = claim["edi_file__uploaded_at"]
            billed_837 = claim["total_charge_amount"]
            billed_835 = matches[0]["total_charge_amount"] if matches else None
            difference = abs(billed_837 - billed_835) if billed_835 is not None else None
            if difference == 0:
                continue
            output.append({
                "key": json.dumps((*key, claim["id"])), "claim": key[0], "internal_number": key[1],
                "record_837_id": claim["id"], "record_835_id": matches[0]["id"] if matches else None,
                "waiting_days": max(0, (period["today"] - timezone.localtime(received, zone).date()).days),
                "billed_837": billed_837, "billed_835": billed_835, "difference": difference,
                "category": "both" if matches else "837-only",
                "status": "Billed amounts differ" if matches else "835 not received",
                "source_837": claim["edi_file__original_filename"],
                "source_835": matches[0]["edi_file__original_filename"] if matches else "",
            })
    return output, ambiguous


@authenticated_api_required
@require_GET
def comparison_835_837(request):
    client, denied = only_client_user(request)
    if denied:
        return denied
    try:
        period = resolve_period(request.GET, client)
        page_size = min(100, max(1, int(request.GET.get("page_size", 10))))
        page = max(1, int(request.GET.get("page", 1)))
    except (InvalidPeriod, ValueError, TypeError) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    category = request.GET.get("category", "both")
    band = request.GET.get("age_band", "all")
    sort = request.GET.get("sort", "waiting_days")
    direction = request.GET.get("direction", "desc")
    output_format = request.GET.get("format", "json")
    if (category not in {"both", "837-only"} or (band != "all" and band not in AGE_RANGES)
            or sort not in dict(COLUMNS) or direction not in {"asc", "desc"}
            or output_format not in {"json", "csv"}):
        return JsonResponse({"success": False, "error": "Invalid comparison filter."}, status=400)
    rows, ambiguous = exception_rows(client, period)
    rows = [row for row in rows if row["category"] == category]
    if band != "all":
        low, high = AGE_RANGES[band]
        rows = [row for row in rows if row["waiting_days"] >= low and (high is None or row["waiting_days"] < high)]
    rows = sorted((row for row in rows if row[sort] is not None),
                  key=lambda row: (row[sort], row["key"]), reverse=direction == "desc") + sorted(
                      (row for row in rows if row[sort] is None), key=lambda row: row["key"])
    summary = age_summary(rows)
    for row in rows:
        for field in MONEY_FIELDS:
            row[field] = format(row[field], ".2f") if row[field] is not None else None
    if output_format == "csv":
        writer = csv.writer(_CsvSink(), lineterminator="\r\n")
        def stream():
            yield "\ufeff"
            yield writer.writerow([label for _, label in COLUMNS])
            for row in rows:
                yield writer.writerow([csv_safe_text(str(row[key])) if row[key] is not None else "" for key, _ in COLUMNS])
        response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="filex-835-837_{category}_{band}.csv"'
    else:
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        page = min(page, pages)
        response = JsonResponse({"success": True, "period": period_payload(period),
            "category": category, "age_band": band, "age_summary": summary,
            "count": len(rows), "page": page, "pages": pages, "page_size": page_size,
            "ambiguous_claims": ambiguous, "rows": rows[(page - 1) * page_size:page * page_size]})
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response
