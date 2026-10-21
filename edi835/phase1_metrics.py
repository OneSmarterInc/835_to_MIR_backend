"""Aggregate operational evidence without returning source documents or identifiers."""
from datetime import timezone as datetime_timezone

from django.db.models import Avg, BigIntegerField, Count, F, Func, Max, Min, Q, Sum
from django.db.models.functions import TruncDay, TruncHour


class ByteLength(Func):
    """Database-side byte count; never transfer the source to Python."""

    function = "OCTET_LENGTH"
    output_field = BigIntegerField()

    def as_sqlite(self, compiler, connection, **extra_context):
        return super().as_sql(
            compiler, connection, template="LENGTH(CAST(%(expressions)s AS BLOB))",
            **extra_context,
        )


def peak_bucket(queryset, timestamp: str, truncation) -> dict:
    peak = (
        queryset.order_by()
        .annotate(bucket=truncation(timestamp, tzinfo=datetime_timezone.utc))
        .values("bucket").annotate(records=Count("pk"))
        .order_by("-records", "bucket").first()
    )
    return {"start_utc": peak["bucket"].isoformat(), "records": peak["records"]} if peak else {
        "start_utc": None, "records": 0,
    }


def stream_metrics(queryset, timestamp: str, *, size_field: str | None = None,
                   content_field: str | None = None, claim_field: str | None = None,
                   duration_fields: tuple[str, str] | None = None) -> dict:
    """Counts are saved records, not necessarily distinct physical submissions."""
    aggregates = {"records": Count("pk"), "unassigned_records": Count("pk", filter=Q(client__isnull=True))}
    if claim_field:
        aggregates["recorded_claims"] = Sum(claim_field, default=0)
    result = queryset.aggregate(**aggregates)
    result["statuses"] = dict(
        queryset.order_by().values("status").annotate(records=Count("pk"))
        .values_list("status", "records")
    )
    result["peak_utc_clock_hour"] = peak_bucket(queryset, timestamp, TruncHour)
    result["peak_utc_calendar_day"] = peak_bucket(queryset, timestamp, TruncDay)
    result["bytes"] = None
    if content_field or size_field:
        expression = ByteLength(content_field) if content_field else F(size_field)
        sized = queryset.annotate(measured_bytes=expression).filter(measured_bytes__gt=0)
        sizes = sized.aggregate(
            measured_records=Count("pk"), total=Sum("measured_bytes", default=0),
            minimum=Min("measured_bytes"), maximum=Max("measured_bytes"),
            mean=Avg("measured_bytes"),
        )
        sizes["zero_or_unavailable_records"] = result["records"] - sizes["measured_records"]
        sizes["source"] = "database_content_bytes" if content_field else "stored_size_metadata"
        result["bytes"] = sizes
    result["persisted_interval_seconds"] = None
    if duration_fields:
        start, end = duration_fields
        valid = queryset.filter(**{f"{end}__gte": F(start)})
        times = valid.aggregate(
            measured_records=Count("pk"), mean=Avg(F(end) - F(start)),
            maximum=Max(F(end) - F(start)), minimum=Min(F(end) - F(start)),
        )
        for key in ("mean", "maximum", "minimum"):
            times[key] = times[key].total_seconds() if times[key] is not None else None
        times["negative_intervals"] = queryset.filter(**{f"{end}__lt": F(start)}).count()
        times["missing_timestamps"] = result["records"] - times["measured_records"] - times["negative_intervals"]
        times["start_field"], times["end_field"] = start, end
        result["persisted_interval_seconds"] = times
    return result
