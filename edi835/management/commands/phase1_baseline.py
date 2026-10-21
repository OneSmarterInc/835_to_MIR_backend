"""Read-only, aggregate baseline for Phase 1 discovery; not a load generator."""
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from accounts.models import Client
from edi835.models import AnalysisUpload, EDI835File, EDI837File, MIRFile, RECONFile, SFTPAutomationRun
from edi835.phase1_metrics import stream_metrics


def host_snapshot() -> dict:
    """Local filesystem capacity only; no paths, secrets, source names or contents."""
    path = Path(settings.MEDIA_ROOT).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    usage = shutil.disk_usage(path)
    return {
        "logical_cpus": os.cpu_count(),
        "media_filesystem_total_bytes": usage.total,
        "media_filesystem_free_bytes": usage.free,
        "note": "Host-visible snapshot, not container quota, application memory or proven throughput.",
    }


class Command(BaseCommand):
    help = "Emit aggregate Phase 1 evidence as JSON without modifying application data."
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--as-of", help="Exclusive end timestamp, ISO 8601 with timezone; default now.")
        parser.add_argument("--client-id", type=UUID)
        parser.add_argument("--include-content-sizes", action="store_true",
                            help="Also calculate bytes for 835/Analysis sources in the DB (extra disk/CPU cost).")
        parser.add_argument("--include-host", action="store_true")
        parser.add_argument("--statement-timeout-ms", type=int, default=15000)

    def handle(self, *args, **options):
        if not 1 <= options["days"] <= 366:
            raise CommandError("--days must be between 1 and 366.")
        if not 100 <= options["statement_timeout_ms"] <= 120000:
            raise CommandError("--statement-timeout-ms must be between 100 and 120000.")
        try:
            end = datetime.fromisoformat(options["as_of"]) if options["as_of"] else timezone.now()
        except ValueError as exc:
            raise CommandError("--as-of must be an ISO 8601 timestamp with a timezone.") from exc
        if timezone.is_naive(end):
            raise CommandError("--as-of must include a timezone.")
        end = end.astimezone(datetime_timezone.utc)
        start = end - timedelta(days=options["days"])
        client_id = options["client_id"]
        include_sizes = options["include_content_sizes"]
        began = time.monotonic()
        report = {
            "schema_version": 1, "generated_at_utc": timezone.now().isoformat(),
            "scope": "one_client" if client_id else "all_clients",
            "window": {"start_inclusive_utc": start.isoformat(), "end_exclusive_utc": end.isoformat()},
            "database_vendor": connection.vendor, "streams": {},
            "limitations": [
                "Saved records are not physical arrivals: combined 835 batches can be one record; rejected unsaved files are absent.",
                "Clock-hour/day peaks are not rolling-window peaks. No names, claim identifiers or source content are returned.",
                "Persisted intervals may include queue wait, I/O, retries and delivery; they are not isolated converter CPU times.",
                "Analysis has no processing-duration timestamps; missing metrics are null, not zero.",
                "This is historical evidence, not a capacity/load test or backup/restore verification.",
            ],
        }
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                                   [str(options["statement_timeout_ms"])])
            if client_id and not Client.objects.filter(pk=client_id).exists():
                raise CommandError("The selected client does not exist.")
            specifications = [
                ("835", EDI835File, "uploaded_at", {"claim_field": "claims_count",
                 "content_field": "input_file_content" if include_sizes else None,
                 "duration_fields": ("processing_started_at", "processing_completed_at")}, {}),
                ("837", EDI837File, "uploaded_at", {"claim_field": "claim_count", "size_field": "file_size",
                 "duration_fields": ("uploaded_at", "processed_at")}, {}),
                ("RECON", RECONFile, "uploaded_at", {"claim_field": "claim_count", "size_field": "file_size",
                 "duration_fields": ("processing_started_at", "processed_at")}, {"file_kind": "RECON"}),
                ("legacy_837_reference", RECONFile, "uploaded_at", {"claim_field": "claim_count",
                 "size_field": "file_size"}, {"file_kind": "837"}),
                ("MIR_output", MIRFile, "converted_at", {"claim_field": "claim_count", "size_field": "file_size"}, {}),
                ("analysis_835", AnalysisUpload, "uploaded_at", {"claim_field": "accepted_count",
                 "content_field": "raw_content" if include_sizes else None}, {"kind": "835"}),
                ("analysis_837", AnalysisUpload, "uploaded_at", {"claim_field": "accepted_count",
                 "content_field": "raw_content" if include_sizes else None}, {"kind": "837"}),
                ("automation_runs", SFTPAutomationRun, "scheduled_for",
                 {"duration_fields": ("started_at", "finished_at")}, {}),
            ]
            for name, model, timestamp, metrics, filters in specifications:
                filters.update({f"{timestamp}__gte": start, f"{timestamp}__lt": end})
                if client_id:
                    filters["client_id"] = client_id
                report["streams"][name] = stream_metrics(model.objects.filter(**filters), timestamp, **metrics)
        if options["include_host"]:
            report["host"] = host_snapshot()
        report["collection_seconds"] = round(time.monotonic() - began, 3)
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
