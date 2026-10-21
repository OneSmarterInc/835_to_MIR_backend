import json
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from accounts.models import Client
from edi835.models import AnalysisUpload, EDI835File, EDI837File, RECONFile
from edi835.services import sync_folder_observer
from edi835.storage import client_storage_dirs


class Phase1BaselineTests(TestCase):
    def setUp(self):
        self.client_a = Client.objects.create(name="A", client_code="BASE-A", email="a@example.com")
        self.client_b = Client.objects.create(name="B", client_code="BASE-B", email="b@example.com")
        self.end = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def report(self, **options):
        output = StringIO()
        call_command("phase1_baseline", as_of=self.end.isoformat(), days=1, stdout=output, **options)
        return json.loads(output.getvalue())

    def source(self, client, at, *, content="", duration=10):
        record = EDI835File.objects.create(client=client, original_filename="private-name.835", claims_count=2)
        EDI835File.objects.filter(pk=record.pk).update(
            uploaded_at=at, input_file_content=content,
            processing_started_at=at, processing_completed_at=at + timedelta(seconds=duration),
        )
        return record

    def test_window_scope_peaks_intervals_and_no_writes_or_document_reads(self):
        start = self.end - timedelta(days=1)
        self.source(self.client_a, start, duration=10)
        self.source(self.client_a, start + timedelta(minutes=20), duration=30)
        self.source(self.client_a, start + timedelta(hours=1), duration=-1)
        self.source(self.client_a, self.end)
        self.source(self.client_a, start - timedelta(microseconds=1))
        self.source(self.client_b, start)
        with CaptureQueriesContext(connection) as queries:
            data = self.report(client_id=self.client_a.pk)
        result = data["streams"]["835"]
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["recorded_claims"], 6)
        self.assertEqual(result["peak_utc_clock_hour"]["records"], 2)
        self.assertEqual(result["peak_utc_calendar_day"]["records"], 3)
        self.assertEqual(result["persisted_interval_seconds"]["mean"], 20)
        self.assertEqual(result["persisted_interval_seconds"]["negative_intervals"], 1)
        self.assertEqual(result["persisted_interval_seconds"]["missing_timestamps"], 0)
        self.assertIsNone(result["bytes"])
        self.assertNotIn("private-name", json.dumps(data))
        self.assertNotIn(str(self.client_a.pk), json.dumps(data))
        for query in queries:
            sql = query["sql"].upper()
            self.assertFalse(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")))
            self.assertNotIn('"INPUT_FILE_CONTENT"', sql)
            self.assertNotIn('"RAW_CONTENT"', sql)
            self.assertNotIn('"FILE_CONTENT"', sql)

    def test_opt_in_sizes_are_bytes_and_remain_aggregates(self):
        self.source(self.client_a, self.end - timedelta(hours=1), content="é£")
        AnalysisUpload.objects.create(client=self.client_a, filename="hidden.835", kind="835",
                                      content_sha256="a", raw_content=b"abcde",
                                      uploaded_at=self.end - timedelta(hours=1), accepted_count=3)
        data = self.report(include_content_sizes=True)
        self.assertEqual(data["streams"]["835"]["bytes"]["total"], 4)
        self.assertEqual(data["streams"]["analysis_835"]["bytes"]["total"], 5)
        self.assertEqual(data["streams"]["analysis_835"]["recorded_claims"], 3)
        self.assertNotIn("hidden.835", json.dumps(data))
        self.assertNotIn("abcde", json.dumps(data))

    def test_metadata_sizes_unknown_durations_and_legacy_reference_separation(self):
        record = EDI837File.objects.create(client=self.client_a, original_filename="x", file_size=123)
        EDI837File.objects.filter(pk=record.pk).update(uploaded_at=self.end - timedelta(hours=1))
        ref = RECONFile.objects.create(client=self.client_a, original_filename="ref", file_kind="837")
        RECONFile.objects.filter(pk=ref.pk).update(uploaded_at=self.end - timedelta(hours=1))
        data = self.report()
        self.assertEqual(data["streams"]["837"]["bytes"]["maximum"], 123)
        self.assertEqual(data["streams"]["837"]["persisted_interval_seconds"]["missing_timestamps"], 1)
        self.assertIsNone(data["streams"]["837"]["persisted_interval_seconds"]["mean"])
        self.assertEqual(data["streams"]["RECON"]["records"], 0)
        self.assertEqual(data["streams"]["legacy_837_reference"]["records"], 1)

    def test_empty_database_has_no_invented_measurements(self):
        result = self.report()["streams"]["835"]
        self.assertEqual(result["records"], 0)
        self.assertEqual(result["peak_utc_clock_hour"], {"start_utc": None, "records": 0})
        self.assertIsNone(result["persisted_interval_seconds"]["maximum"])

    def test_invalid_arguments_fail_without_queries(self):
        for options in ({"days": 0}, {"days": 367}, {"as_of": "2026-10-21"},
                        {"as_of": "bad"}, {"statement_timeout_ms": 0}):
            with self.subTest(options=options), self.assertNumQueries(0), self.assertRaises(CommandError):
                call_command("phase1_baseline", stdout=StringIO(), **options)

    def test_unknown_client_is_not_reported_as_an_empty_valid_scope(self):
        from uuid import uuid4
        with self.assertRaisesMessage(CommandError, "does not exist"):
            self.report(client_id=uuid4())


class FolderObserverTenantTests(TestCase):
    def test_same_filename_is_independent_per_client_and_observer_is_repeatable(self):
        a = Client.objects.create(name="A", client_code="OBS-A", email="a@example.com")
        b = Client.objects.create(name="B", client_code="OBS-B", email="b@example.com")
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            for client in (a, b, None):
                (client_storage_dirs(client)["835_in"] / "same.835").write_text("synthetic")
            for client in (a, b, None, a, b, None):
                sync_folder_observer(client)
            for client in (a, b, None):
                self.assertEqual(EDI835File.objects.filter(client=client, original_filename="same.835").count(), 1)
