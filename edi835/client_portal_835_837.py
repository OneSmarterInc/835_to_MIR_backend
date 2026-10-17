"""Client-scoped billed-charge comparisons for an 837 arrival cohort."""
from collections import defaultdict
from datetime import date
import csv
import json
from zoneinfo import ZoneInfo

from django.db.models import BigIntegerField, Case, F, OuterRef, Q, Subquery, Value, When
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from project835.decorators import authenticated_api_required
from .claim_numbers import split_claim_number
from .client_portal_comparison import AGE_RANGES, age_summary, csv_safe_text, _CsvSink
from .client_portal_period import InvalidPeriod, resolve_period, apply_period, period_payload
from .client_portal_views import only_client_user
from .models import EDI835Claim, EDI837Claim


COLUMNS = (("claim", "Claim"), ("waiting_days", "Waiting days"),
           ("source_type", "File type"), ("filename", "Source file"),
           ("received_at", "Received"), ("internal_number", "Internal claim"), ("billed_837", "Billed 837"),
           ("billed_835", "Billed 835"), ("difference", "Billed difference"),
           ("status", "835/837 status"))
EXPORT_COLUMNS = (*COLUMNS, ("file_id", "File ID"), ("claim_sequence", "Claim occurrence"))
SORT_FIELDS = {*dict(COLUMNS), "source_837", "source_835", "priority"}
MONEY_FIELDS = {"billed_837", "billed_835", "difference"}


def occurrence(row: dict, kind: str, zone: ZoneInfo) -> dict:
    """One physical claim record, retaining file identity even when names repeat."""
    return {
        "key": f'{kind}:{row["id"]}', "record_id": row["id"], "source_type": kind,
        "file_id": str(row["edi_file_id"]), "claim_sequence": row["claim_sequence"],
        "filename": row["edi_file__original_filename"],
        "received_at": timezone.localtime(row["edi_file__uploaded_at"], zone).isoformat(),
        "internal_number": (submission_identity(row) if kind == "837" else
                            identity(row["highmark_claim_number"], row["internal_claim_number"]))[1],
        "billed_837": row["total_charge_amount"] if kind == "837" else None,
        "billed_835": row["total_charge_amount"] if kind == "835" else None,
    }


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


def exception_rows(client, period: dict, *, search: str = "", category: str | None = None) -> tuple[list[dict], int]:
    submissions = defaultdict(list)
    remittances = defaultdict(list)
    all_submission_keys = defaultdict(set)
    identifiers = ("id", "highmark_claim_number", "internal_claim_number", "claim_control_number")
    file_fields = ("edi_file_id", "claim_sequence", "edi_file__uploaded_at", "edi_file__original_filename", "total_charge_amount")
    fields = (*identifiers, *file_fields)
    remittance_fields = ("id", "highmark_claim_number", "internal_claim_number", *file_fields)
    # Resolve identity ambiguity over all history, not just the selected day.
    all_837 = EDI837Claim.objects.filter(client=client, edi_file__client=client)
    # Resolve identity using lightweight metadata, never billed amounts. Search
    # must not hide sibling occurrences and turn an ambiguous pair into a match.
    selected_837 = apply_period(all_837, "edi_file__uploaded_at", period)
    for row in selected_837.values(*identifiers).iterator(chunk_size=1500):
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
    all_835 = EDI835Claim.objects.filter(edi_file__client=client).exclude(
        edi_file__ingestion_source="HELD_RELEASE"
    )
    for row in all_835.values("id", "highmark_claim_number", "internal_claim_number").iterator(chunk_size=1500):
        key = identity(row["highmark_claim_number"], row["internal_claim_number"])
        if key[0] in all_submission_keys:
            remittances[key[0]].append((key, row))
    pairs, missing, ambiguous_numbers = {}, [], set()
    keys = {row["id"]: key for key, rows in submissions.items() for row in rows}
    for key, submitted in submissions.items():
        candidates = remittances.get(key[0], [])
        exact = [row for match, row in candidates if match == key]
        if exact:
            matches = exact
        elif candidates and len(all_submission_keys[key[0]]) == 1:
            # REF*9C in 837 and the internal reference in 835 need not agree.
            # Only fall back to the exact external claim number when unique;
            # multiple 835 occurrences remain ambiguous below.
            matches = [row for _, row in candidates]
        elif candidates:
            ambiguous_numbers.add(key[0])
            continue
        else:
            matches = []
        # Repeated claim records cannot establish a one-to-one billed comparison.
        if (matches and len(submitted) != 1) or len(matches) > 1:
            ambiguous_numbers.add(key[0])
            continue
        for claim in submitted:
            if matches:
                pairs[claim["id"]] = matches[0]["id"]
            else:
                missing.append(claim["id"])

    # If any identity under a claim number is ambiguous, show the complete
    # group once instead of repeating a remittance across guessed pairings.
    pairs = {pk: remittance for pk, remittance in pairs.items() if keys[pk][0] not in ambiguous_numbers}
    missing = [pk for pk in missing if keys[pk][0] not in ambiguous_numbers]
    details = selected_837
    if search:
        hits = selected_837.filter(
            Q(highmark_claim_number__icontains=search) | Q(internal_claim_number__icontains=search)
            | Q(claim_control_number__icontains=search) | Q(edi_file__original_filename__icontains=search)
        )
        numbers = {keys[pk][0] for pk in hits.values_list("id", flat=True)}
        received_hits = all_835.filter(
            Q(highmark_claim_number__icontains=search) | Q(internal_claim_number__icontains=search)
            | Q(edi_file__original_filename__icontains=search)
        ).values_list("highmark_claim_number", flat=True)
        numbers.update(identity(number, "")[0] for number in received_hits)
        pairs = {pk: remittance for pk, remittance in pairs.items() if keys[pk][0] in numbers}
        missing = [pk for pk in missing if keys[pk][0] in numbers]
        ambiguous_numbers.intersection_update(numbers)
    records = []
    if category != "837-only":
        pair_items = list(pairs.items())
        # Bounded CASE mappings preserve the exact, ambiguity-checked pairing.
        # 100 pairs keep repeated subquery bind parameters below SQLite's limit
        # as well as avoiding an unbounded SQL statement on PostgreSQL.
        for start in range(0, len(pair_items), 100):
            batch = pair_items[start:start + 100]
            candidates = details.filter(pk__in=[pk for pk, _ in batch]).annotate(
                _record_835_id=Case(
                    *(When(pk=pk, then=Value(remittance)) for pk, remittance in batch),
                    output_field=BigIntegerField(),
                ),
            ).annotate(
                _billed_835=Subquery(all_835.filter(pk=OuterRef("_record_835_id")).values("total_charge_amount")[:1]),
            ).filter(_billed_835__isnull=False).exclude(total_charge_amount=F("_billed_835"))
            # Signed Decimal equality is excluded by SQL, before amounts or
            # source details are returned to Python, pagination, chart or CSV.
            records.extend(candidates.values(*fields, "_record_835_id", "_billed_835"))
    if category != "both":
        for start in range(0, len(missing), 500):
            records.extend(details.filter(pk__in=missing[start:start + 500]).values(*fields))
    source_ids = list({row["_record_835_id"] for row in records if row.get("_record_835_id")})
    source_records = {}
    for start in range(0, len(source_ids), 500):
        source_records.update({row["id"]: row for row in all_835.filter(pk__in=source_ids[start:start + 500])
                               .values(*remittance_fields)})
    output = []
    zone = ZoneInfo(period["timezone"])
    for claim in records:
        key = keys[claim["id"]]
        received = claim["edi_file__uploaded_at"]
        billed_837 = claim["total_charge_amount"]
        billed_835 = claim.get("_billed_835")
        record_835_id = claim.get("_record_835_id")
        output.append({
            "key": json.dumps((*key, claim["id"])), "claim": key[0], "internal_number": key[1],
            "record_837_id": claim["id"], "record_835_id": record_835_id,
            "waiting_days": max(0, (period["today"] - timezone.localtime(received, zone).date()).days),
            "billed_837": billed_837, "billed_835": billed_835,
            "difference": abs(billed_837 - billed_835) if billed_835 is not None else None,
            "category": "both" if record_835_id else "837-only",
            "status": "Billed amounts differ" if record_835_id else "835 not received",
            "source_837": claim["edi_file__original_filename"],
            "source_835": source_records[record_835_id]["edi_file__original_filename"] if record_835_id else "",
            "ambiguous": False,
            "occurrences": [occurrence(claim, "837", zone)] + (
                [occurrence(source_records[record_835_id], "835", zone)] if record_835_id else []),
        })
    if category != "837-only" and ambiguous_numbers:
        grouped = defaultdict(list)
        submitted_ids = [pk for pk, key in keys.items() if key[0] in ambiguous_numbers]
        received_ids = {row["id"] for number in ambiguous_numbers for _, row in remittances[number]}
        for kind, ids, query, columns in (
            ("837", submitted_ids, selected_837, fields),
            ("835", list(received_ids), all_835, remittance_fields),
        ):
            for start in range(0, len(ids), 500):
                for row in query.filter(pk__in=ids[start:start + 500]).values(*columns):
                    number = keys[row["id"]][0] if kind == "837" else identity(row["highmark_claim_number"], "")[0]
                    grouped[number].append(occurrence(row, kind, zone))
        for number, occurrences in grouped.items():
            occurrences.sort(key=lambda item: (item["source_type"] != "837", item["received_at"], item["file_id"], item["claim_sequence"], item["record_id"]))
            arrivals = [item["received_at"][:10] for item in occurrences if item["source_type"] == "837"]
            if not arrivals:
                continue
            output.append({
                "key": "multiple:" + number, "claim": number, "internal_number": "",
                "record_837_id": None, "record_835_id": None,
                "waiting_days": max(0, (period["today"] - date.fromisoformat(min(arrivals))).days),
                "billed_837": None, "billed_835": None, "difference": None,
                "category": "both", "status": "Multiple files — review", "ambiguous": True,
                "source_837": next((item["filename"] for item in occurrences if item["source_type"] == "837"), ""),
                "source_835": next((item["filename"] for item in occurrences if item["source_type"] == "835"), ""),
                "occurrences": occurrences,
            })
    return output, len(ambiguous_numbers) if category != "837-only" else 0


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
    search = request.GET.get("search", "").strip()
    if (category not in {"both", "837-only"} or (band != "all" and band not in AGE_RANGES)
            or sort not in SORT_FIELDS or direction not in {"asc", "desc"}
            or output_format not in {"json", "csv"} or len(search) > 100):
        return JsonResponse({"success": False, "error": "Invalid comparison filter."}, status=400)
    rows, _ = exception_rows(client, period, search=search, category=category)
    rows = [row for row in rows if row["category"] == category]
    by_claim = defaultdict(list)
    for row in rows:
        by_claim[row["claim"]].append(row)
    groups = list(by_claim.values())
    # A claim stays together through age filtering, sorting and pagination.
    # Its age is measured from the oldest selected 837 occurrence.
    for group in groups:
        days = max(row["waiting_days"] for row in group)
        for row in group:
            row["waiting_days"] = days
    if band != "all":
        low, high = AGE_RANGES[band]
        groups = [group for group in groups if group[0]["waiting_days"] >= low and (high is None or group[0]["waiting_days"] < high)]
    effective_sort = "waiting_days" if sort == "priority" else sort

    def sort_value(group):
        if effective_sort in {"source_type", "filename", "received_at", "internal_number", "billed_835", "billed_837"}:
            values = [item[effective_sort] for row in group for item in row["occurrences"] if item[effective_sort] is not None]
        else:
            values = [row[effective_sort] for row in group if row[effective_sort] is not None]
        return (max(values) if direction == "desc" else min(values)) if values else None

    groups = sorted((group for group in groups if sort_value(group) is not None),
                    key=lambda group: (sort_value(group), group[0]["claim"]), reverse=direction == "desc") + sorted(
                        (group for group in groups if sort_value(group) is None), key=lambda group: group[0]["claim"])
    summary = age_summary([group[0] for group in groups])
    ambiguous = sum(any(row["ambiguous"] for row in group) for group in groups)
    rows = [row for group in groups for row in group]
    occurrence_count = sum(len(row["occurrences"]) for row in rows)
    amount_summary = {
        "matched": sum(row["difference"] == 0 for row in rows),
        "mismatched": sum(row["difference"] is not None and row["difference"] != 0 for row in rows),
        "missing_835": sum(row["category"] == "837-only" for row in rows),
    }
    for row in rows:
        for field in MONEY_FIELDS:
            row[field] = format(row[field], ".2f") if row[field] is not None else None
        for item in row["occurrences"]:
            for field in ("billed_835", "billed_837"):
                item[field] = format(item[field], ".2f") if item[field] is not None else None
    if output_format == "csv":
        writer = csv.writer(_CsvSink(), lineterminator="\r\n")
        def stream():
            yield "\ufeff"
            yield writer.writerow([label for _, label in EXPORT_COLUMNS])
            for row in rows:
                for item in row["occurrences"]:
                    record = {**row, **item}
                    yield writer.writerow([csv_safe_text(str(record[key])) if record[key] is not None else "" for key, _ in EXPORT_COLUMNS])
        response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="filex-835-837_{category}_{band}.csv"'
    else:
        pages = max(1, (len(groups) + page_size - 1) // page_size)
        page = min(page, pages)
        response = JsonResponse({"success": True, "period": period_payload(period),
            "category": category, "age_band": band, "age_summary": summary,
            "amount_summary": amount_summary,
            "count": len(groups), "occurrence_count": occurrence_count,
            "page": page, "pages": pages, "page_size": page_size,
            "ambiguous_claims": ambiguous,
            "rows": [row for group in groups[(page - 1) * page_size:page * page_size] for row in group]})
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response
