import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import Client, User
from .client_portal_analysis import analysis_upload, parse_analysis
from .models import AnalysisClaim, AnalysisUpload
from project835.middleware import AdminAccessMiddleware


def envelope(kind="837", segments=None, element="*", terminator="~"):
    isa = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *261017*1200*^*00501*000000001*0*P*:"
    segments = segments or (["CLM*123456789ABC001*100", "REF*9C*INTERNAL"] if kind == "837" else ["CLP*123456789*1*80*50*0*ZZ*ABC001"])
    content = [isa, f"GS*{'HC' if kind == '837' else 'HP'}*SENDER*RECEIVER*20261017*1200*1*X*005010X222A1",
               f"ST*{kind}*0001", *segments, f"SE*{len(segments) + 2}*0001", "GE*1*1", "IEA*1*000000001"]
    return (terminator.join(content) + terminator).replace("*", element)


class AnalysisParserTests(SimpleTestCase):
    def test_minimal_normalized_payload_and_identity(self):
        parsed = parse_analysis(envelope(segments=["CLM*123456789ABC001*100", "REF*9C*INTERNAL",
            "NM1*QC*1*PRIVATE*PATIENT", "LX*1", "REF*9C*SERVICE-ID"]), "837")
        self.assertEqual(parsed, [{"claim": "123456789", "internal_number": "INTERNAL", "billed": "100.00", "sequence": 1}])
        self.assertNotIn("PRIVATE", json.dumps(parsed))

    def test_custom_delimiters_signed_zero_and_duplicate_records(self):
        parsed = parse_analysis(envelope("835", ["CLP*123*1*-100.01*0*0*ZZ*ID", "CLP*123*1*0*0*0*ZZ*ID"], "|", "!"), "835")
        self.assertEqual([row["billed"] for row in parsed], ["-100.01", "0.00"])
        self.assertEqual([row["sequence"] for row in parsed], [1, 2])

    def test_invalid_amounts_are_never_silently_zero(self):
        for value in ("", "NaN", "Infinity", "1e3", "1,000", "1.123", "x", "12345678901234567"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_analysis(envelope("835", [f"CLP*123*1*{value}*0*0*ZZ*ID"]), "835")

    def test_allowed_amount_includes_patient_responsibility(self):
        cases = [("657", "0", "657", "657.00"), ("657", "500", "157", "657.00"),
                 ("657", "500", "100", "600.00"), ("-657", "-500", "-157", "-657.00"),
                 ("0", "0", "0", "0.00"), ("1", "0.10", "0.20", "0.30")]
        for billed, payment, responsibility, expected in cases:
            with self.subTest(payment=payment, responsibility=responsibility):
                row = parse_analysis(envelope("835", [f"CLP*123*1*{billed}*{payment}*{responsibility}*ZZ*ID"]), "835")[0]
                self.assertEqual(row["allowed"], expected)
                self.assertEqual(row["payment"], format(Decimal(payment), ".2f"))

    def test_missing_patient_responsibility_is_unknown(self):
        row = parse_analysis(envelope("835", ["CLP*123*1*657*0**ZZ*ID"]), "835")[0]
        self.assertEqual(row["payment"], "0.00")
        self.assertIsNone(row["patient_responsibility"])
        self.assertIsNone(row["allowed"])

    def test_invalid_payment_or_responsibility_rejected(self):
        for payment, responsibility in (("", "0"), ("NaN", "0"), ("0", "wrong"), ("0", "1.123")):
            with self.subTest(payment=payment, responsibility=responsibility), self.assertRaises(ValueError):
                parse_analysis(envelope("835", [f"CLP*123*1*657*{payment}*{responsibility}*ZZ*ID"]), "835")

    def test_wrong_type_and_malformed_envelopes(self):
        valid = envelope()
        for bad in (valid.replace("SE*4*0001", "SE*3*0001"), valid.replace("GE*1*1", "GE*2*1"),
                    valid.replace("IEA*1*000000001", "IEA*1*000000002"), valid.replace("SE*4*0001", "SE*4*0002"),
                    valid[:-1], valid + valid, valid[:90], "CLM*123*100~", envelope("835")):
            with self.subTest(content=bad[-35:]), self.assertRaises(ValueError):
                parse_analysis(bad, "837")

    def test_multiple_transactions_and_padded_counts(self):
        content = envelope().replace("GE*1*1", "ST*837*0002~CLM*SECOND*50~SE*0003*0002~GE*02*1")
        self.assertEqual(len(parse_analysis(content, "837")), 2)
        with self.assertRaises(ValueError):
            parse_analysis(content.replace("0002", "0001"), "837")

    def test_limits_and_empty_claims(self):
        with patch("edi835.client_portal_analysis.MAX_CLAIMS", 1), self.assertRaises(ValueError):
            parse_analysis(envelope(segments=["CLM*A*1", "CLM*B*1"]), "837")
        with self.assertRaises(ValueError):
            parse_analysis(envelope(segments=["BHT*0019*00"]), "837")
        with patch("edi835.client_portal_analysis.MAX_SEGMENTS", 2), self.assertRaises(ValueError):
            parse_analysis(envelope(), "837")


class AnalysisUploadTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Analysis", client_code="ANALYSIS", email="analysis@example.com")
        self.user = User.objects.create_user(email="viewer@example.com", name="Viewer", mobile="15555550135",
            client=self.tenant, client_role="CLIENT_USER", password="TestPassword!234")
        self.factory = RequestFactory()

    def request(self, kind="837", raw=None, user=None, csrf=True, filename="claims.x12"):
        request = self.factory.post("/edi835/api/client-portal/analysis/", {
            "kind": kind, "file": SimpleUploadedFile(filename, (raw or envelope(kind)).encode()),
        })
        request.user = user or self.user
        request._dont_enforce_csrf_checks = csrf
        return analysis_upload(request)

    def get(self, user=None, query=""):
        request = self.factory.get("/edi835/api/client-portal/analysis/" + query)
        request.user = user or self.user
        return analysis_upload(request)

    def test_atomic_staging_save_without_production_writes(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.request()
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertTrue(body["saved"])
        self.assertFalse(body["reused"])
        self.assertEqual(body["file"]["claims"][0]["billed"], "100.00")
        self.assertEqual(set(body["file"]), {"id", "filename", "kind", "uploaded_at", "claims"})
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertTrue(writes)
        self.assertTrue(all("client_analysis_" in sql for sql in writes))
        record = AnalysisUpload.objects.get()
        self.assertEqual(bytes(record.raw_content), envelope().encode())
        self.assertEqual(record.uploaded_by, self.user)
        self.assertEqual(record.claims.count(), 1)
        self.assertIn("no-store", response["Cache-Control"])

    def test_extensionless_sources_validate_and_save_by_content(self):
        for kind in ("837", "835"):
            with self.subTest(kind=kind):
                filename = f"CLAIMS_{kind}_20261017"
                response = self.request(kind, filename=filename)
                self.assertEqual(response.status_code, 200)
                body = json.loads(response.content)
                self.assertTrue(body["saved"])
                self.assertEqual(body["file"]["filename"], filename)
                record = AnalysisUpload.objects.get(filename=filename)
                self.assertEqual(record.kind, kind)
                self.assertEqual(bytes(record.raw_content), envelope(kind).encode())
                self.assertEqual(record.claims.count(), 1)
        count = AnalysisUpload.objects.count()
        for raw in ("not X12", envelope("835")):
            response = self.request("837", raw=raw, filename="INVALID_SOURCE")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(AnalysisUpload.objects.count(), count)

    def test_client_user_upload_through_role_middleware(self):
        other = Client.objects.create(name="Other", client_code="OTHER-UPLOAD", email="other-upload@example.com")
        middleware = AdminAccessMiddleware(analysis_upload)
        for kind in ("837", "835"):
            request = self.factory.post("/edi835/api/client-portal/analysis/", {
                "kind": kind, "client_id": str(other.pk),
                "file": SimpleUploadedFile(f"CLAIMS_{kind}", envelope(kind).encode()),
            })
            request.user = self.user
            request.session = {}
            request._dont_enforce_csrf_checks = True
            response = middleware(request)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(json.loads(response.content)["saved"])
        self.assertEqual(AnalysisUpload.objects.filter(client=self.tenant).count(), 2)
        self.assertFalse(AnalysisUpload.objects.filter(client=other).exists())

    def test_saved_data_survives_get_and_contains_both_sources(self):
        self.request()
        self.request("835", raw=envelope("835", ["CLP*123456789*1*657*0*657*ZZ*ID"]))
        with CaptureQueriesContext(connection) as queries:
            page1 = json.loads(self.get().content)
        page2 = json.loads(self.get(query="?after=" + page1["next_cursor"]).content)
        self.assertEqual(page1["files"][0]["kind"], "837")
        self.assertEqual(page2["files"][0]["kind"], "835")
        self.assertEqual(page2["files"][0]["claims"][0]["allowed"], "657.00")
        self.assertIsNone(page2["next_cursor"])
        self.assertFalse(any('raw_content' in q['sql'] for q in queries))
        self.assertEqual(self.get(query="?after=bad").status_code, 400)

    def test_tenant_isolation_and_empty_staging(self):
        self.request()
        other = Client.objects.create(name="Other", client_code="OTHER", email="other@example.com")
        self.user.client = other
        with CaptureQueriesContext(connection) as queries:
            response = self.get()
        self.assertEqual(json.loads(response.content)["files"], [])
        # Analysis has no fallback to any existing ingestion table.
        self.assertFalse(any('edi835_edi' in q['sql'] or 'edi837_claim' in q['sql'] for q in queries))
        self.request("835")
        self.assertEqual(AnalysisUpload.objects.filter(client=other).count(), 1)
        self.assertEqual(AnalysisUpload.objects.filter(client=self.tenant).count(), 1)

    def test_identical_upload_is_idempotent_but_changed_content_is_preserved(self):
        first = json.loads(self.request().content)
        retry = json.loads(self.request().content)
        self.assertEqual(first["file"]["id"], retry["file"]["id"])
        self.assertTrue(retry["reused"])
        self.assertEqual(AnalysisClaim.objects.count(), 1)
        self.request(raw=envelope(segments=["CLM*CHANGED*101"]))
        self.request(filename="other.837")
        self.assertEqual(AnalysisUpload.objects.count(), 3)

    def test_failure_rolls_back_source_and_claims_and_can_retry(self):
        with patch("edi835.client_portal_analysis.AnalysisClaim.objects.bulk_create", side_effect=DatabaseError("storage failure")):
            self.assertEqual(self.request().status_code, 503)
        self.assertEqual(AnalysisUpload.objects.count(), 0)
        self.assertEqual(AnalysisClaim.objects.count(), 0)
        self.assertEqual(self.request().status_code, 200)
        self.assertEqual(AnalysisUpload.objects.count(), 1)

    def test_invalid_file_never_enters_staging(self):
        self.assertEqual(self.request(raw="invalid").status_code, 400)
        self.assertEqual(AnalysisUpload.objects.count(), 0)
        self.assertEqual(AnalysisClaim.objects.count(), 0)

    def test_permissions_and_csrf(self):
        self.assertEqual(self.request(user=AnonymousUser()).status_code, 401)
        self.assertEqual(self.request(csrf=False).status_code, 403)
        self.user.client_role = "CLIENT_ADMIN"
        self.assertEqual(self.request().status_code, 403)

    def test_get_token_and_post_with_csrf(self):
        get = self.factory.get("/edi835/api/client-portal/analysis/")
        get.user = self.user
        response = analysis_upload(get)
        token = json.loads(response.content)["csrf_token"]
        post = self.factory.post("/edi835/api/client-portal/analysis/", {
            "kind": "835", "file": SimpleUploadedFile("payer.835", envelope("835").encode()),
        }, HTTP_X_CSRFTOKEN=token)
        post.user = self.user
        post.COOKIES[settings.CSRF_COOKIE_NAME] = get.META["CSRF_COOKIE"]
        self.assertEqual(analysis_upload(post).status_code, 200)

    def test_bad_input_and_size_limit(self):
        self.assertEqual(self.request(raw="invalid").status_code, 400)
        self.assertEqual(self.request(kind="835", raw=envelope("837")).status_code, 400)
        with patch("edi835.client_portal_analysis.MAX_BYTES", 10):
            self.assertEqual(self.request().status_code, 413)
