"""Database-side Analysis comparisons over tenant-scoped staging projections.

Only summary rows leave the list endpoint. Clinical/source records are fetched
separately for a selected claim or in bounded batches during CSV export.
"""
import csv
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from decimal import Decimal

from django.db import connection
from django.db.models import Count
from django.db.models.functions import Trim, Upper

from accounts.models import Client
from .models import AnalysisClaim, AnalysisUpload


AMOUNTS = {
    "billed_837": ("837", "billed"), "billed_835": ("835", "billed"),
    "payment": ("835", "payment"),
    "patient_responsibility": ("835", "patient_responsibility"),
    "allowed": ("835", "payment + patient_responsibility"),
}
SORTS = {"claim", "internal_number", "filename", "difference", "allowed_difference", *AMOUNTS}
MISMATCH = "n835 > 0 AND NOT COALESCE(n837 = 1 AND n835 = 1 AND a837 = a835 AND a837 = allowed_value, FALSE)"


def query_options(params: Mapping[str, str]) -> dict[str, Any]:
    category = params.get("category", "auto")
    sort = params.get("sort", "claim")
    direction = params.get("direction", "asc")
    search = params.get("search", "").strip()
    try:
        page, size = int(params.get("page", "1")), int(params.get("page_size", "10"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid page.") from exc
    if (category not in {"auto", "both", "837-only"} or sort not in SORTS
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
    amount_columns = []
    for key, (kind, expression) in AMOUNTS.items():
        test = f"kind = '{kind}'"
        amount_columns.extend([
            f"MIN(CASE WHEN {test} THEN {expression} END) AS {key}_value",
            f"COUNT(DISTINCT CASE WHEN {test} THEN {expression} END) + "
            f"MAX(CASE WHEN {test} AND ({expression}) IS NULL THEN 1 ELSE 0 END) AS {key}_count",
        ])
    sql = f"""WITH source AS (
        SELECT UPPER(TRIM(c.claim_number)) AS claim, u.kind, u.filename,
               UPPER(TRIM(c.internal_number)) AS internal_number,
               {cents('billed')} AS billed, {cents('payment')} AS payment,
               {cents('patient_responsibility')} AS patient_responsibility
        FROM client_analysis_claim c JOIN client_analysis_upload u ON u.id = c.upload_id
        WHERE u.client_id = %s {claim_filter}
    ), distinct_records AS (
        SELECT DISTINCT claim, kind, filename, internal_number, billed, payment, patient_responsibility
        FROM source
    ), grouped AS (
        SELECT claim,
            SUM(CASE WHEN kind = '837' THEN 1 ELSE 0 END) AS n837,
            SUM(CASE WHEN kind = '835' THEN 1 ELSE 0 END) AS n835,
            MIN(CASE WHEN kind = '837' THEN billed END) AS a837,
            MIN(CASE WHEN kind = '835' THEN billed END) AS a835,
            COALESCE(MIN(CASE WHEN kind = '835' THEN NULLIF(internal_number, '') END), '') AS internal_number,
            COUNT(DISTINCT CASE WHEN kind = '835' THEN NULLIF(internal_number, '') END) AS reference_count,
            MIN(CASE WHEN kind = '837' THEN filename END) AS filename,
            MAX({search_sql}) AS search_match,
            {', '.join(amount_columns)}
        FROM distinct_records GROUP BY claim
    ), compared AS (
        SELECT grouped.*,
            CASE WHEN n837 = 1 AND n835 = 1 THEN ABS(a837 - a835) END AS difference,
            CASE WHEN n837 = 1 AND n835 = 1 THEN ABS(a837 - allowed_value) END AS allowed_difference,
            CASE WHEN n837 = 1 THEN a837 END AS billed_837,
            CASE WHEN n835 = 1 THEN a835 END AS billed_835,
            CASE WHEN n837 = 1 AND n835 = 1 THEN payment_value END AS payment,
            CASE WHEN n837 = 1 AND n835 = 1 THEN patient_responsibility_value END AS patient_responsibility,
            CASE WHEN n837 = 1 AND n835 = 1 THEN allowed_value END AS allowed
        FROM grouped WHERE n837 > 0 AND search_match = 1
    ) """
    return sql, params


def money(cents: int | Decimal | None) -> str | None:
    return None if cents is None else format(Decimal(cents) / 100, ".2f")


def summary_row(record: dict[str, Any], has835: bool) -> dict[str, Any]:
    paired = record["n837"] == record["n835"] == 1
    ambiguous = record["n835"] > 0 and not paired

    def status(delta):
        if ambiguous:
            return "Multiple occurrences — review"
        if not record["n835"]:
            return "Not in uploaded 835" if has835 else "Awaiting 835 upload"
        return "Amount unavailable" if delta is None else "Match" if delta == 0 else "Mismatch"

    return {
        "key": record["claim"], "claim": record["claim"], "internal_number": record["internal_number"],
        "reference_count": record["reference_count"], "waiting_days": 0,
        "category": "both" if record["n835"] else "837-only", "ambiguous": ambiguous,
        **{key: money(record[key]) for key in AMOUNTS},
        "difference": money(record["difference"]), "allowed_difference": money(record["allowed_difference"]),
        "billed_status": status(record["difference"]), "allowed_status": status(record["allowed_difference"]),
        "status": status(record["difference"]), "source_837": "", "source_835": "", "record_835_id": None,
        "display_amounts": {key: {"count": record[f"{key}_count"], "value": money(record[f"{key}_value"])} for key in AMOUNTS},
        "occurrences": [],
    }


def dictionaries(cursor, rows):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in rows]


def selection(options: dict[str, Any]) -> str:
    where = MISMATCH if options["category"] == "both" else "n835 = 0"
    # All identifiers come from the allowlist in query_options, never request SQL.
    sort = options["sort"]
    direction = options["direction"].upper()
    return f"FROM compared WHERE {where} ORDER BY {sort} {direction} NULLS LAST, claim ASC"


def comparison_page(client: Client, options: dict[str, Any]) -> dict[str, Any]:
    counts = file_counts(client)
    options = dict(options)
    if options["category"] == "auto":
        options["category"] = "both" if counts["835"] else "837-only"
    sql, params = comparison_sql(client, options["search"])
    total_column = "count_both" if options["category"] == "both" else "count_missing"
    # Counts and visible rows share one SQL snapshot. The grouped CTE is reused,
    # so we don't repeat the full comparison scan for pagination totals.
    sql += f""", totals AS (
        SELECT COUNT(CASE WHEN {MISMATCH} THEN 1 END) AS count_both,
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
      ORDER BY paged.{options['sort']} {options['direction'].upper()} NULLS LAST, paged.claim ASC"""
    with connection.cursor() as cursor:
        cursor.execute(sql, [*params, options["size"], options["page"], options["page"], options["size"], options["size"]])
        records = dictionaries(cursor, cursor.fetchall())
    metadata = records[0]
    rows = [summary_row(record, counts["835"] > 0) for record in records if record["claim"] is not None]
    return {"rows": rows, "total": metadata["total"],
            "counts": {"both": metadata["count_both"], "837-only": metadata["count_missing"]},
            "file_counts": counts, "page": metadata["page"], "pages": metadata["pages"], "category": options["category"]}


def claim_occurrences(client: Client, numbers: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """One narrow query for selected claims, never the uploaded raw file."""
    records = (AnalysisClaim.objects.filter(upload__client=client)
               .annotate(number=Upper(Trim("claim_number")))
               .filter(number__in=numbers).order_by("upload_id", "sequence")
               .values("id", "number", "upload_id", "sequence", "internal_number", "billed", "payment",
                       "patient_responsibility", "upload__kind", "upload__filename", "upload__uploaded_at"))
    result, seen = {}, set()
    for record in records.iterator(chunk_size=1000):
        kind, filename = record["upload__kind"], record["upload__filename"]
        identity = (record["number"], kind, filename, record["internal_number"].strip().upper(),
                    record["billed"], record["payment"], record["patient_responsibility"])
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
    sql, params = comparison_sql(client, claim=number)
    with connection.cursor() as cursor:
        cursor.execute(sql + "SELECT * FROM compared", params)
        records = dictionaries(cursor, cursor.fetchall())
    if not records:
        return None
    row = summary_row(records[0], AnalysisUpload.objects.filter(client=client, kind="835").exists())
    row["occurrences"] = claim_occurrences(client, [row["claim"]]).get(row["claim"], [])
    return row


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
                items = occurrences.get(row["claim"], [])
                sources = list({(item["source_type"], item["filename"]): item for item in reversed(items)}.values())
                def source(kind, key):
                    return " | ".join(item[key] for item in sources if item["source_type"] == kind)
                def amounts(key):
                    kind = "837" if key == "billed_837" else "835"
                    return " | ".join(dict.fromkeys(item[key] or "Unknown" for item in items if item["source_type"] == kind))
                values = [row["claim"], source("837", "filename"), source("835", "filename"),
                          source("837", "received_at"), source("835", "received_at"),
                          " | ".join(dict.fromkeys(item["internal_number"] for item in items if item["internal_number"])),
                          *[amounts(key) for key in AMOUNTS], row["difference"], row["billed_status"],
                          row["allowed_difference"], row["allowed_status"]]
                yield writer.writerow([safe_csv(value) for value in values])
