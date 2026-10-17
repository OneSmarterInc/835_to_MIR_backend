import csv
import json
from decimal import Decimal
from io import StringIO
from time import perf_counter
from unittest import skipUnless

from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import Client, User
from .analysis_queries import claim_detail, comparison_csv, comparison_page, query_options
from .client_portal_analysis import analysis_upload
from .models import AnalysisClaim, AnalysisClaimRejection, AnalysisUpload


class AnalysisQueryTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(name="Query", client_code="QUERY", email="query@example.com")
        self.user = User.objects.create_user(email="queries@example.com", name="Viewer", mobile="15555550235",
                                            client=self.client_record, client_role="CLIENT_USER", password="TestPassword!234")
        self.counter = 0

    def upload(self, kind, claims, filename=None, tenant=None):
        self.counter += 1
        upload = AnalysisUpload.objects.create(client=tenant or self.client_record, kind=kind,
            filename=filename or f"file-{self.counter}.{kind}", content_sha256=str(self.counter), raw_content=b"private",
            accepted_count=len(claims), processing_version=2)
        for index, values in enumerate(claims, 1):
            number, billed, payment, responsibility, internal = (*values, *([None] * (5 - len(values))))
            AnalysisClaim.objects.create(upload=upload, sequence=index, claim_number=number, billed=Decimal(billed),
                payment=payment, patient_responsibility=responsibility, internal_number=internal or "REF")
        return upload

    def page(self, **options):
        return comparison_page(self.client_record, query_options(options))

    def get(self, **params):
        request = RequestFactory().get("/edi835/api/client-portal/analysis/", params)
        request.user = self.user
        return analysis_upload(request)

    def test_signed_comparisons_unknowns_and_complete_group_pagination(self):
        self.upload("837", [("MATCH", "657"), ("BILLED", "100"), ("ALLOWED", "657"), ("SIGNED", "100"),
                            ("UNKNOWN", "100"), ("MISSING", "1"), ("CENTS", ".30")])
        self.upload("835", [("MATCH", "657", "0", "657"), ("BILLED", "80", "70", "30"),
                            ("ALLOWED", "657", "600", "0"), ("SIGNED", "-100", "-100", "0"),
                            ("UNKNOWN", "100", "10", None), ("CENTS", ".30", ".10", ".20")])
        result = self.page()
        self.assertEqual(result["counts"], {"both": 4, "837-only": 1})
        rows = {row["claim"]: row for row in result["rows"]}
        self.assertEqual(rows["SIGNED"]["difference"], "200.00")
        self.assertEqual(rows["BILLED"]["allowed_status"], "Match")
        self.assertEqual(rows["ALLOWED"]["billed_status"], "Match")
        self.assertEqual(rows["UNKNOWN"]["allowed_status"], "Amount unavailable")
        self.assertEqual(rows["UNKNOWN"]["occurrences"], [])
        self.assertNotIn("filename", rows["BILLED"])
        self.assertEqual(self.page(category="837-only")["rows"][0]["claim"], "MISSING")

    def test_distinct_filename_financial_records_and_display_values(self):
        self.upload("837", [(" claim ", "100", None, None, " r "), ("CLAIM", "100", None, None, "R")], "same.837")
        self.upload("837", [("CLAIM", "100", None, None, "R")], "same.837")
        self.upload("835", [("CLAIM", "80", "50", "20", "R")], "same.835")
        row = self.page()["rows"][0]
        self.assertFalse(row["ambiguous"])
        self.assertEqual(row["difference"], "20.00")
        self.assertEqual(len(claim_detail(self.client_record, "claim")["occurrences"]), 2)
        self.upload("835", [("CLAIM", "90", "50", "20", "OTHER")], "other.835")
        row = self.page()["rows"][0]
        self.assertTrue(row["ambiguous"])
        self.assertEqual(row["reference_count"], 2)
        self.assertEqual(row["display_amounts"]["billed_835"]["count"], 2)
        self.assertEqual(row["display_amounts"]["payment"], {"count": 1, "value": "50.00"})
        self.assertEqual(len(claim_detail(self.client_record, "CLAIM")["occurrences"]), 3)

    def test_search_keeps_entire_group_and_treats_wildcards_literally(self):
        self.upload("837", [("X", "100", None, None, "837-REF")], "percent%_file.837")
        self.upload("835", [("X", "80", "50", "0", "835-REF")], "response.835")
        for search in ("837-ref", "835-ref", "percent%_", "response", "X"):
            row = self.page(search=search)["rows"][0]
            self.assertEqual(row["category"], "both")
            self.assertEqual(row["difference"], "20.00")
        self.assertEqual(self.page(search="notfound")["total"], 0)
        self.assertEqual(self.page(search="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.page(search="%%%%")["total"], 0)

    def test_constant_queries_bounded_payload_paging_sort_and_full_csv(self):
        self.upload("837", [(f"C{i:03}", str(i)) for i in range(51)])
        self.upload("835", [(f"C{i:03}", "99", "90", "0") for i in range(51)])
        with CaptureQueriesContext(connection) as queries:
            result = self.page(page="2", sort="billed_837", direction="desc")
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual(result["total"], 51)
        self.assertEqual(result["rows"][0]["claim"], "C040")
        self.assertTrue(all(not row["occurrences"] for row in result["rows"]))
        self.assertFalse(any('"raw_content"' in query["sql"] for query in queries))
        self.assertEqual(self.page(page="999")["page"], 6)
        with CaptureQueriesContext(connection) as export_queries:
            text = "".join(comparison_csv(self.client_record, query_options({"category": "both", "page": "2"})))
        self.assertEqual(len(export_queries), 3, "Export batches sources without per-claim queries")
        records = list(csv.reader(StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(len(records), 52)
        self.assertEqual(records[1][6:11], ["0.00", "99.00", "90.00", "0.00", "90.00"])
        text = "".join(comparison_csv(self.client_record, query_options({"category": "both", "search": "C050"})))
        self.assertEqual(len(list(csv.reader(StringIO(text)))), 2)

    def test_private_reads_lazy_reports_and_input_validation(self):
        other = Client.objects.create(name="Other", client_code="OTHERQ", email="otherq@example.com")
        foreign = self.upload("837", [("SECRET", "100")], tenant=other)
        local = self.upload("837", [("LOCAL", "100")])
        self.upload("835", [("LOCAL", "100", "100", "0")], tenant=other)
        AnalysisClaimRejection.objects.create(upload=local, sequence=3, claim_number="BAD", reason="Invalid amount")
        local.rejected_count = 1
        local.save(update_fields=["rejected_count"])
        response = self.get(view="comparison")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("SECRET", response.content.decode())
        self.assertNotIn("Invalid amount", response.content.decode())
        self.assertEqual(json.loads(response.content)["counts"], {"both": 0, "837-only": 1})
        self.assertEqual(self.get(view="claim", claim="SECRET").status_code, 404)
        self.assertEqual(self.get(view="rejections", upload=str(foreign.pk)).status_code, 404)
        self.assertEqual(self.get(view="rejection-export", upload=str(foreign.pk)).status_code, 404)
        result = json.loads(self.get(view="rejections", upload=str(local.pk)).content)
        self.assertEqual(result["rejections"][0]["reason"], "Invalid amount")
        self.assertTrue(self.get(view="rejection-export", upload=str(local.pk)).streaming)
        self.assertTrue(self.get(view="export", category="837-only").streaming)
        for invalid in ({"sort": "claim; DROP TABLE"}, {"page": "0"}, {"page_size": "999"}, {"direction": "invalid"}, {"search": "x" * 101}):
            self.assertEqual(self.get(view="comparison", **invalid).status_code, 400)
        detail = json.loads(self.get(view="claim", claim="LOCAL").content)["row"]
        self.assertEqual(detail["occurrences"][0]["filename"], local.filename)

    def test_empty_dataset_and_missing_status(self):
        self.assertEqual(self.page()["rows"], [])
        self.upload("837", [("A", "100")])
        self.assertEqual(self.page()["rows"][0]["billed_status"], "Awaiting 835 upload")
        self.upload("835", [])
        self.assertEqual(self.page(category="837-only")["rows"][0]["billed_status"], "Not in uploaded 835")

    @skipUnless(connection.vendor == "postgresql", "Exact maximum NUMERIC precision requires PostgreSQL")
    def test_postgres_maximum_precision_and_sign(self):
        self.upload("837", [("CENT", "9999999999999999.99"), ("NEGATIVE", "9999999999999999.99")])
        self.upload("835", [("CENT", "9999999999999999.98", "9999999999999999.98", "0"),
                            ("NEGATIVE", "-9999999999999999.99", "-9999999999999999.99", "-9999999999999999.99")])
        rows = {row["claim"]: row for row in self.page()["rows"]}
        self.assertEqual(rows["CENT"]["difference"], "0.01")
        self.assertEqual(rows["NEGATIVE"]["allowed_difference"], "29999999999999999.97")

    def test_large_history_request_and_payload_reduction(self):
        from .client_portal_analysis import upload_payload
        uploads = [AnalysisUpload(client=self.client_record, kind=kind, filename=f"{index}.{kind}",
                   content_sha256=str(index), raw_content=b"private", accepted_count=200, processing_version=2)
                   for kind in ("837", "835") for index in range(50)]
        AnalysisUpload.objects.bulk_create(uploads)
        claims = []
        for upload in uploads:
            index = int(upload.filename.split(".")[0])
            for sequence in range(200):
                claims.append(AnalysisClaim(upload=upload, sequence=sequence + 1, claim_number=f"C{index * 200 + sequence:05}",
                    internal_number="REF", billed=Decimal("100" if upload.kind == "837" else "80"),
                    payment=Decimal("70") if upload.kind == "835" else None,
                    patient_responsibility=Decimal("10") if upload.kind == "835" else None))
        AnalysisClaim.objects.bulk_create(claims, batch_size=1000)
        start = perf_counter()
        response = self.get(view="comparison")
        elapsed = perf_counter() - start
        result = json.loads(response.content)
        self.assertEqual(result["total"], 10000)
        self.assertEqual(len(result["rows"]), 10)
        old_bytes = sum(len(json.dumps(upload_payload(upload)).encode()) for upload in uploads)
        new_bytes = len(response.content)
        self.assertLess(new_bytes, old_bytes // 100)
        print(f"\nAnalysis benchmark ({connection.vendor}, synthetic): 100 files / 20,000 occurrences; "
              f"100 history requests -> 1 page request; {old_bytes:,} -> {new_bytes:,} payload bytes; "
              f"new request {elapsed:.3f}s.")

    def test_csv_injection_missing_status_and_limited_rejection_preview(self):
        upload = self.upload("837", [("=FORMULA", "10")], "+filename.837")
        exported = "".join(comparison_csv(self.client_record, query_options({"category": "837-only"})))
        self.assertIn("'=FORMULA", exported)
        self.assertIn("'+filename.837", exported)
        self.assertIn("Awaiting 835 upload", exported)
        AnalysisClaimRejection.objects.bulk_create([AnalysisClaimRejection(upload=upload, sequence=i,
            reason=f"Invalid amount {i}") for i in range(25)])
        upload.rejected_count = 25
        upload.save(update_fields=["rejected_count"])
        preview = json.loads(self.get(view="rejections", upload=str(upload.pk)).content)
        self.assertEqual(len(preview["rejections"]), 20)
        self.assertEqual(preview["total"], 25)
        report = self.get(view="rejection-export", upload=str(upload.pk))
        text = b"".join(report.streaming_content).decode("utf-8-sig")
        self.assertEqual(len(list(csv.reader(StringIO(text)))), 26)
