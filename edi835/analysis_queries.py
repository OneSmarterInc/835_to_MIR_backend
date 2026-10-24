"""Database-side Analysis comparisons over tenant-scoped staging projections.

Only summary rows leave the list endpoint. Clinical/source records are fetched
separately for a selected claim or in bounded batches during CSV export.
"""
import csv
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from decimal import Decimal

from django.db import connection
from django.db.models import Count, Q
from django.db.models.functions import Trim, Upper

from accounts.models import Client
from .models import AnalysisClaim, AnalysisUpload, ClaimAdjustmentReason, RemittanceCodeCrosswalk


AMOUNTS = {
    "billed_837": ("837", "billed"), "billed_835": ("835", "billed"),
    "payment": ("835", "payment"),
    "patient_responsibility": ("835", "patient_responsibility"),
    "allowed": ("835", "payment + patient_responsibility"),
}
SORTS = {"claim", "internal_number", "filename", "difference", "allowed_difference", *AMOUNTS}
MATCH = "COALESCE(n837 = 1 AND n835 > 0 AND has_reversal = 0 AND (a837 = payment_value OR a837 = allowed_value), FALSE)"
MISMATCH = f"n835 > 0 AND NOT {MATCH}"


def query_options(params: Mapping[str, str]) -> dict[str, Any]:
    category = params.get("category", "auto")
    sort = params.get("sort", "claim")
    direction = params.get("direction", "asc")
    search = params.get("search", "").strip()
    try:
        page, size = int(params.get("page", "1")), int(params.get("page_size", "10"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid page.") from exc
    if (category not in {"auto", "both", "matched", "837-only"} or sort not in SORTS
            or direction not in {"asc", "desc"} or not 1 <= page <= 1000000000 or size not in {10, 25, 50}
            or len(search) > 100):
        raise ValueError("Invalid comparison filters.")
    return dict(category=category, sort=sort, direction=direction, search=search, page=page, size=size)


def file_counts(client: Client) -> dict[str, int]:
    counts = {"837": 0, "835": 0}
    counts.update({row["kind"]: row["total"] for row in
                   AnalysisUpload.objects.filter(client=client).values("kind").annotate(total=Count("pk"))})
    return counts


def comparison_sql(client: Client, search: str = "", claim: str | None = None) -> tuple[str, list[Any]]:
    # Integer cents avoid binary float comparisons on the SQLite test backend;
    # PostgreSQL performs the multiplication/rounding using exact NUMERIC.
    def cents(field: str) -> str:
        return f"CAST(ROUND(c.{field} * 100, 0) AS BIGINT)"
    tenant = AnalysisUpload._meta.get_field("client").target_field.get_db_prep_value(client.pk, connection)
    params = [tenant]
    claim_filter = ""
    if claim is not None:
        claim_filter = " AND UPPER(TRIM(c.claim_number)) = %s"
        params.append(claim.strip().upper())
    search_sql = "1"
    if search:
        pattern = "%" + connection.ops.prep_for_like_query(search.upper()) + "%"
        search_sql = "CASE WHEN claim LIKE %s ESCAPE '\\' OR internal_number LIKE %s ESCAPE '\\' OR UPPER(filename) LIKE %s ESCAPE '\\' THEN 1 ELSE 0 END"
        params.extend([pattern] * 3)
    # Upload order is the business ordering across files; original CLP sequence
    # selects the final entry within a file. Never infer state from amount size.
    latest_billed = "MAX(CASE WHEN kind = '835' AND claim_rank = 1 THEN billed END)"
    json_array = "JSON_AGG" if connection.vendor == "postgresql" else "JSON_GROUP_ARRAY"
    amount_columns = []
    for key, (kind, expression) in AMOUNTS.items():
        test = f"kind = '{kind}'"
        if key == "billed_835":
            amount_columns.extend([
                f"{latest_billed} AS billed_835_value",
                "MAX(CASE WHEN kind = '835' THEN 1 ELSE 0 END) AS billed_835_count",
            ])
        elif kind == "835":
            # SUM normally ignores NULL. A missing component must make its total
            # unknown, otherwise an incomplete remittance could falsely match.
            amount_columns.extend([
                f"CASE WHEN MAX(CASE WHEN {test} AND ({expression}) IS NULL THEN 1 ELSE 0 END) = 0 "
                f"THEN SUM(CASE WHEN {test} THEN {expression} END) END AS {key}_value",
                f"MAX(CASE WHEN {test} THEN 1 ELSE 0 END) AS {key}_count",
            ])
        else:
            amount_columns.extend([
                f"MIN(CASE WHEN {test} THEN {expression} END) AS {key}_value",
                f"COUNT(DISTINCT CASE WHEN {test} THEN {expression} END) + "
                f"MAX(CASE WHEN {test} AND ({expression}) IS NULL THEN 1 ELSE 0 END) AS {key}_count",
            ])
    sql = f"""WITH source AS (
        SELECT UPPER(TRIM(c.claim_number)) AS claim, u.kind, u.filename, c.payment_date, u.uploaded_at, u.id AS upload_id, c.sequence, c.id AS record_id,
               COALESCE(n.claim_status, '') AS claim_status, COALESCE(i.fingerprint, '') AS fingerprint,
               UPPER(TRIM(c.internal_number)) AS internal_number,
               {cents('billed')} AS billed, {cents('payment')} AS payment,
               {cents('patient_responsibility')} AS patient_responsibility
        FROM client_analysis_claim c JOIN client_analysis_upload u ON u.id = c.upload_id
        LEFT JOIN client_analysis_835_claim n ON n.analysis_claim_id = c.id
        LEFT JOIN client_analysis_claim_identity i ON i.claim_id = c.id
        WHERE u.client_id = %s {claim_filter}
    ), ranked_records AS (
        SELECT source.*, ROW_NUMBER() OVER (
            PARTITION BY claim, kind, filename, internal_number, billed, payment, patient_responsibility, payment_date, claim_status, fingerprint,
                CASE WHEN kind = '835' AND internal_number = '' THEN record_id ELSE 0 END
            ORDER BY uploaded_at DESC, upload_id DESC, sequence DESC
        ) AS record_rank FROM source
    ), distinct_records AS (
        SELECT ranked_records.*, CASE WHEN internal_number != '' THEN 'id:' || internal_number
            ELSE 'missing:' || CAST(record_id AS TEXT) END AS reference_key
        FROM ranked_records WHERE record_rank = 1
    ), reference_lists AS (
        SELECT claim, {json_array}(internal_number) AS refs FROM (
            SELECT DISTINCT claim, internal_number FROM distinct_records
            WHERE kind = '835' AND internal_number != '' ORDER BY claim, internal_number
        ) refs GROUP BY claim
    ), source_lists AS (
        SELECT claim, kind, {json_array}(filename) AS filenames FROM (
            SELECT DISTINCT claim, kind, filename FROM distinct_records ORDER BY claim, kind, filename
        ) names GROUP BY claim, kind
    ), dated AS (
        SELECT distinct_records.*, ROW_NUMBER() OVER (
            PARTITION BY claim, kind ORDER BY uploaded_at DESC, upload_id DESC, sequence DESC
        ) AS claim_rank, ROW_NUMBER() OVER (
            PARTITION BY claim, kind, reference_key ORDER BY uploaded_at DESC, upload_id DESC, sequence DESC
        ) AS internal_rank FROM distinct_records
    ), grouped AS (
        SELECT claim,
            SUM(CASE WHEN kind = '837' THEN 1 ELSE 0 END) AS n837,
            SUM(CASE WHEN kind = '835' THEN 1 ELSE 0 END) AS n835,
            MIN(CASE WHEN kind = '837' THEN billed END) AS a837,
            {latest_billed} AS a835,
            MAX(CASE WHEN kind = '835' AND claim_rank = 1 THEN payment_date END) AS latest_payment_date,
            MAX(CASE WHEN kind = '835' THEN uploaded_at END) AS latest_uploaded_at,
            '' AS group_key,
            COALESCE(MIN(CASE WHEN kind = '835' THEN internal_number END), '') AS internal_number,
            MAX(CASE WHEN kind = '835' AND internal_rank = 1 AND claim_status = '22' THEN 1 ELSE 0 END) AS has_reversal,
            MAX(CASE WHEN kind = '835' AND claim_rank = 1 THEN claim_status END) AS latest_claim_status,
            MAX(CASE WHEN kind = '835' AND claim_rank = 1 THEN record_id END) AS selected_record_id,
            MIN(CASE WHEN kind = '837' THEN filename END) AS filename,
            MAX({search_sql}) AS search_match,
            {', '.join(amount_columns)}
        FROM dated GROUP BY claim
    ), compared AS (
        SELECT grouped.*, r.refs AS internal_numbers,
            s837.filenames AS sources_837, s835.filenames AS sources_835,
            CASE WHEN n837 = 1 AND n835 > 0 THEN ABS(a837 - a835) END AS difference,
            CASE WHEN n837 = 1 AND n835 > 0 THEN
                CASE WHEN a837 = payment_value THEN 0 ELSE ABS(a837 - allowed_value) END
            END AS allowed_difference,
            CASE WHEN a837 = payment_value THEN 'payment' ELSE 'payment_and_responsibility' END AS allowed_basis,
            CASE WHEN n837 = 1 THEN a837 END AS billed_837,
            a835 AS billed_835,
            payment_value AS payment,
            patient_responsibility_value AS patient_responsibility,
            allowed_value AS allowed
        FROM grouped
        LEFT JOIN reference_lists r ON r.claim = grouped.claim
        LEFT JOIN source_lists s837 ON s837.claim = grouped.claim AND s837.kind = '837'
        LEFT JOIN source_lists s835 ON s835.claim = grouped.claim AND s835.kind = '835'
        WHERE n837 > 0 AND search_match = 1
    ) """
    return sql, params


def money(cents: int | Decimal | None) -> str | None:
    return None if cents is None else format(Decimal(cents) / 100, ".2f")


def json_values(value: Any) -> list[str]:
    return sorted(json.loads(value) if isinstance(value, str) else value or [])


def summary_row(record: dict[str, Any], has835: bool) -> dict[str, Any]:
    paired = record["n837"] == 1 and record["n835"] > 0
    ambiguous = record["n835"] > 0 and not paired

    def status(delta):
        if ambiguous:
            return "Multiple occurrences — review"
        if not record["n835"]:
            return "Not in uploaded 835" if has835 else "Awaiting 835 upload"
        return "Amount unavailable" if delta is None else "Match" if delta == 0 else "Mismatch"

    references = json_values(record["internal_numbers"])
    reversed_claim = bool(record["has_reversal"])
    billed_status = "Reversed — review" if reversed_claim else status(record["difference"])
    return {
        "key": record["claim"], "claim": record["claim"], "internal_number": " | ".join(references),
        "reference_count": len(references), "internal_numbers": references, "waiting_days": 0,
        "category": "both" if record["n835"] else "837-only", "ambiguous": ambiguous,
        **{key: money(record[key]) for key in AMOUNTS},
        "difference": money(record["difference"]), "allowed_difference": money(record["allowed_difference"]),
        "billed_status": billed_status,
        "allowed_status": "Reversed — review" if reversed_claim else status(record["allowed_difference"]), "allowed_basis": record["allowed_basis"],
        "latest_payment_date": str(record["latest_payment_date"]) if record["latest_payment_date"] else None,
        "latest_uploaded_at": str(record["latest_uploaded_at"]) if record["latest_uploaded_at"] else None,
        "latest_claim_status": record["latest_claim_status"] or "", "selected_record_id": record["selected_record_id"],
        "status": billed_status, "source_837": "", "source_835": "", "record_835_id": None,
        "display_amounts": {key: {"count": record[f"{key}_count"], "value": money(record[f"{key}_value"])} for key in AMOUNTS},
        "occurrences": [],
    }


def dictionaries(cursor, rows):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in rows]


def add_denial_codes(client: Client, rows: list[dict[str, Any]]) -> None:
    """Read CAS codes for the visible claims only, including service-level CAS.

    Keep the adjustment group for display, but resolve the reason independently
    of that group. Existing normalized uploads work without reprocessing.
    """
    by_number = {row["claim"]: set() for row in rows}
    remarks = {number: set() for number in by_number}
    if not by_number:
        return
    records = (AnalysisClaim.objects.filter(upload__client=client, upload__kind="835")
               .annotate(number=Upper(Trim("claim_number")))
               .filter(number__in=by_number)
               .values_list("number", "normalized_835__segment_data"))
    for number, data in records.iterator(chunk_size=1000):
        if not isinstance(data, dict):
            continue
        separator = data.get("element_separator") or "*"
        for segment in data.get("segments", []):
            fields = segment.strip().split(separator)
            remark_positions = ((2,) if fields[0] == "LQ" and len(fields) > 1 and fields[1] == "HE" else
                                (5, 20, 21, 22, 23) if fields[0] == "MIA" else
                                (3, 4, 5, 6, 7) if fields[0] == "MOA" else ())
            remarks[number].update(fields[i].strip() for i in remark_positions if i < len(fields) and fields[i].strip())
            if fields[0] != "CAS" or len(fields) < 4:
                continue
            group = fields[1].strip()
            for index in range(2, len(fields) - 1, 3):
                reason = fields[index].strip()
                if group and reason:
                    by_number[number].add(f"{group}-{reason}")
    reason_codes = {code.split("-", 1)[1] for codes in by_number.values() for code in codes}
    reasons = dict(ClaimAdjustmentReason.objects.filter(code__in=reason_codes).values_list("code", "description"))
    remark_codes = set().union(*remarks.values())
    carc_descriptions, rarc_descriptions = {}, {}
    references = RemittanceCodeCrosswalk.objects.filter(Q(carc_code__in=reason_codes) | Q(rarc_code__in=remark_codes)).order_by("source_row")
    for reference in references:
        for mapping, code, description in (
            (carc_descriptions, reference.carc_code, reference.carc_description),
            (rarc_descriptions, reference.rarc_code, reference.rarc_description),
        ):
            if code and description and description not in mapping.setdefault(code, []):
                mapping[code].append(description)

    def description(mapping, code, fallback=None):
        variants = mapping.get(code, [])
        if not variants:
            return fallback
        return variants[0] if len(variants) == 1 else "Source descriptions:\n" + "\n\n".join(variants)
    for row in rows:
        row["denial_codes"] = sorted(by_number[row["claim"]])
        row["denial_reasons"] = [
            {"code": code, "reason": reasons.get(code.split("-", 1)[1])}
            for code in row["denial_codes"]
        ]
        row["carc_codes"] = [
            {"code": code, "description": description(carc_descriptions, code, reasons.get(code))}
            for code in sorted({value.split("-", 1)[1] for value in row["denial_codes"]})
        ]
        row["rarc_codes"] = [
            {"code": code, "description": description(rarc_descriptions, code)}
            for code in sorted(remarks[row["claim"]])
        ]


def selection(options: dict[str, Any]) -> str:
    where = {"both": MISMATCH, "matched": MATCH, "837-only": "n835 = 0"}[options["category"]]
    # All identifiers come from the allowlist in query_options, never request SQL.
    sort = options["sort"]
    direction = options["direction"].upper()
    return f"FROM compared WHERE {where} ORDER BY {sort} {direction} NULLS LAST, claim ASC, internal_number ASC, group_key ASC"


def comparison_page(client: Client, options: dict[str, Any]) -> dict[str, Any]:
    counts = file_counts(client)
    options = dict(options)
    if options["category"] == "auto":
        options["category"] = "both" if counts["835"] else "837-only"
    sql, params = comparison_sql(client, options["search"])
    total_column = {"both": "count_both", "matched": "count_matched", "837-only": "count_missing"}[options["category"]]
    # Counts and visible rows share one SQL snapshot. The grouped CTE is reused,
    # so we don't repeat the full comparison scan for pagination totals.
    sql += f""", totals AS (
        SELECT COUNT(CASE WHEN {MISMATCH} THEN 1 END) AS count_both,
               COUNT(CASE WHEN {MATCH} THEN 1 END) AS count_matched,
               COUNT(CASE WHEN n835 = 0 THEN 1 END) AS count_missing FROM compared
    ), page_counts AS (
        SELECT *, {total_column} AS total,
            CASE WHEN {total_column} = 0 THEN 1 ELSE (({total_column} - 1) / %s) + 1 END AS pages
        FROM totals
    ), pagination AS (
        SELECT *, CASE WHEN %s > pages THEN pages ELSE %s END AS page FROM page_counts
    ), paged AS (
        SELECT * {selection(options)} LIMIT %s OFFSET (SELECT (page - 1) * %s FROM pagination)
    ) SELECT pagination.*, paged.* FROM pagination LEFT JOIN paged ON 1 = 1
      ORDER BY paged.{options['sort']} {options['direction'].upper()} NULLS LAST, paged.claim ASC, internal_number ASC, group_key ASC"""
    with connection.cursor() as cursor:
        cursor.execute(sql, [*params, options["size"], options["page"], options["page"], options["size"], options["size"]])
        records = dictionaries(cursor, cursor.fetchall())
    metadata = records[0]
    rows = [summary_row(record, counts["835"] > 0) for record in records if record["claim"] is not None]
    add_denial_codes(client, rows)
    return {"rows": rows, "total": metadata["total"],
            "counts": {"both": metadata["count_both"], "matched": metadata["count_matched"], "837-only": metadata["count_missing"]},
            "file_counts": counts, "page": metadata["page"], "pages": metadata["pages"], "category": options["category"]}


def claim_occurrences(client: Client, numbers: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """One narrow query for selected claims, never the uploaded raw file."""
    records = (AnalysisClaim.objects.filter(upload__client=client)
               .annotate(number=Upper(Trim("claim_number")))
               .filter(number__in=numbers).order_by("-upload__uploaded_at", "-upload_id", "-sequence")
               .values("id", "number", "upload_id", "sequence", "internal_number", "billed", "payment", "payment_date",
                       "normalized_835__claim_status", "identity__fingerprint", "patient_responsibility", "upload__kind", "upload__filename", "upload__uploaded_at"))
    result, seen = {}, set()
    for record in records.iterator(chunk_size=1000):
        kind, filename = record["upload__kind"], record["upload__filename"]
        identity = (record["number"], kind, filename, record["internal_number"].strip().upper(),
                    record["billed"], record["payment"], record["patient_responsibility"], record["payment_date"], record["normalized_835__claim_status"], record["identity__fingerprint"],
                    record["id"] if kind == "835" and not record["internal_number"].strip() else None)
        if identity in seen:
            continue
        seen.add(identity)
        def amount(field: str) -> str | None:
            return None if record[field] is None else format(record[field], ".2f")
        payment, responsibility = record["payment"], record["patient_responsibility"]
        allowed = None if payment is None or responsibility is None else format(payment + responsibility, ".2f")
        result.setdefault(record["number"], []).append({
            "key": f"{record['upload_id']}:{record['sequence']}", "record_id": record["id"],
            "file_id": str(record["upload_id"]), "source_type": kind, "filename": filename,
            "received_at": record["upload__uploaded_at"].isoformat(),
            "payment_date": record["payment_date"].isoformat() if record["payment_date"] else None,
            "claim_status": record["normalized_835__claim_status"] or "",
            "internal_number": record["internal_number"] if kind == "835" else "",
            "claim_sequence": record["sequence"],
            "billed_837": amount("billed") if kind == "837" else None,
            "billed_835": amount("billed") if kind == "835" else None,
            "payment": amount("payment") if kind == "835" else None,
            "patient_responsibility": amount("patient_responsibility") if kind == "835" else None,
            "allowed": allowed if kind == "835" else None,
        })
    return result


def claim_detail(client: Client, number: str) -> dict[str, Any] | None:
    if number.startswith('["'):
        try:
            parts = json.loads(number)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid claim reference.") from exc
        if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(part, str) and len(part) <= 280 for part in parts):
            raise ValueError("Invalid claim reference.")
        # Previous deep links included the internal ID. They now open the full claim.
        number = parts[0]
    sql, params = comparison_sql(client, claim=number)
    with connection.cursor() as cursor:
        cursor.execute(sql + "SELECT * FROM compared", params)
        records = dictionaries(cursor, cursor.fetchall())
    if not records:
        return None
    if len(records) > 1:
        raise ValueError("Select a specific internal claim from the Analysis table.")
    row = summary_row(records[0], AnalysisUpload.objects.filter(client=client, kind="835").exists())
    add_denial_codes(client, [row])
    row["occurrences"] = group_occurrences(row, claim_occurrences(client, [row["claim"]]).get(row["claim"], []))
    return row


def group_occurrences(row: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return items


class CsvBuffer:
    def write(self, value: str) -> str:
        return value


def safe_csv(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def comparison_csv(client: Client, options: dict[str, Any]) -> Iterator[str]:
    """Stream the complete filtered export; page size never truncates exports."""
    has835 = AnalysisUpload.objects.filter(client=client, kind="835").exists()
    writer = csv.writer(CsvBuffer(), quoting=csv.QUOTE_ALL)
    yield "\ufeff" + writer.writerow([
        "Claim", "837 source file", "835 source file", "837 uploaded (UTC)", "835 uploaded (UTC)", "Internal claim",
        "Billed charges (837 CLM02)", "Billed charges (835 CLP03)", "Claim payment (835 CLP04)",
        "Patient responsibility (835 CLP05 / PR)", "Allowed amount (835 payment + responsibility)",
        "Billed difference (CLM02 vs CLP03)", "Billed comparison status",
        "Allowed difference (CLM02 vs payment + responsibility)", "Allowed comparison status",
    ])
    sql, params = comparison_sql(client, options["search"])
    # A server-side PostgreSQL cursor avoids buffering all comparison groups.
    with connection.chunked_cursor() as cursor:
        cursor.execute(sql + "SELECT * " + selection(options), params)
        while batch := cursor.fetchmany(100):
            rows = [summary_row(row, has835) for row in dictionaries(cursor, batch)]
            occurrences = claim_occurrences(client, [row["claim"] for row in rows])
            for row in rows:
                items = group_occurrences(row, occurrences.get(row["claim"], []))
                sources = list({(item["source_type"], item["filename"]): item for item in reversed(items)}.values())
                def source(kind, key):
                    return " | ".join(item[key] for item in sources if item["source_type"] == kind)
                def amounts(key):
                    if key != "billed_837":
                        return row[key] if row[key] is not None else "Unknown"
                    kind = "837" if key == "billed_837" else "835"
                    return " | ".join(dict.fromkeys(item[key] or "Unknown" for item in items if item["source_type"] == kind))
                values = [row["claim"], source("837", "filename"), source("835", "filename"),
                          source("837", "received_at"), source("835", "received_at"),
                          " | ".join(dict.fromkeys(item["internal_number"] for item in items if item["internal_number"])),
                          *[amounts(key) for key in AMOUNTS], row["difference"], row["billed_status"],
                          row["allowed_difference"], row["allowed_status"]]
                yield writer.writerow([safe_csv(value) for value in values])
