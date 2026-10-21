import json
import hashlib
from io import StringIO
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import Client, User
from .client_portal_analysis import analysis_upload, parse_analysis
from .models import (AnalysisClaim, AnalysisUpload, Analysis835Claim, Analysis837Claim, Analysis837ServiceLine,
                     AnalysisClaimRejection, EDI835Claim, EDI837Claim, EDI837ServiceLine)
from project835.middleware import AdminAccessMiddleware


def envelope(kind="837", segments=None, element="*", terminator="~"):
    isa = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *261017*1200*^*00501*000000001*0*P*:"
    segments = segments or (["CLM*123456789ABC001*100", "REF*9C*INTERNAL"] if kind == "837" else ["CLP*123456789*1*80*50*0*ZZ*ABC001"])
    guide = "005010X222A1" if kind == "837" else "005010X221A1"
    content = [isa, f"GS*{'HC' if kind == '837' else 'HP'}*SENDER*RECEIVER*20261017*1200*1*X*{guide}",
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

    def test_omitted_patient_responsibility_without_pr_adjustments_is_zero(self):
        row = parse_analysis(envelope("835", ["CLP*123*1*657*0**ZZ*ID"]), "835")[0]
        self.assertEqual(row["payment"], "0.00")
        self.assertEqual(row["patient_responsibility"], "0.00")
        self.assertEqual(row["allowed"], "0.00")

    def test_invalid_payment_or_responsibility_rejected(self):
        for payment, responsibility in (("", "0"), ("NaN", "0"), ("0", "wrong"), ("0", "1.123")):
            with self.subTest(payment=payment, responsibility=responsibility), self.assertRaises(ValueError):
                parse_analysis(envelope("835", [f"CLP*123*1*657*{payment}*{responsibility}*ZZ*ID"]), "835")

    def test_wrong_type_and_malformed_envelopes(self):
        valid = envelope()
        for bad in (valid.replace("SE*4*0001", "SE*3*0001"), valid.replace("GE*1*1", "GE*2*1"),
                    valid.replace("IEA*1*000000001", "IEA*1*000000002"), valid.replace("SE*4*0001", "SE*4*0002"),
                    valid[:-1], valid + "JUNK~", valid[:90], "CLM*123*100~", envelope("835")):
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
        self.assertEqual(set(body["file"]), {"id", "filename", "kind", "uploaded_at", "claims", "status", "accepted_count", "rejected_count", "rejections"})
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertTrue(writes)
        self.assertTrue(all("client_analysis_" in sql for sql in writes))
        record = AnalysisUpload.objects.get()
        self.assertEqual(bytes(record.raw_content), envelope().encode())
        self.assertEqual(record.uploaded_by, self.user)
        self.assertEqual(record.claims.count(), 1)
        self.assertIn("no-store", response["Cache-Control"])

    def test_compact_upload_returns_run_counts_without_claim_or_rejection_arrays(self):
        raw = envelope("837", ["CLM*VALID*100", "CLM*BAD*NaN"])
        request = self.factory.post("/edi835/api/client-portal/analysis/?compact=1", {
            "kind": "837", "file": SimpleUploadedFile("compact.837", raw.encode()),
        })
        request.user = self.user
        request._dont_enforce_csrf_checks = True
        response = analysis_upload(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)["file"]
        self.assertEqual(payload["accepted_count"], 1)
        self.assertEqual(payload["rejected_count"], 1)
        self.assertNotIn("claims", payload)
        self.assertNotIn("rejections", payload)
        self.assertEqual(AnalysisClaim.objects.count(), 1)
        self.assertEqual(AnalysisClaimRejection.objects.count(), 1)

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
        retry = self.request()
        self.assertEqual(retry.status_code, 409)
        self.assertIn("Duplicate file", json.loads(retry.content)["error"])
        self.assertTrue(AnalysisUpload.objects.filter(pk=first["file"]["id"]).exists())
        self.assertEqual(AnalysisClaim.objects.count(), 1)
        self.assertEqual(self.request(raw=envelope(segments=["CLM*CHANGED*101"])).status_code, 200)
        self.assertEqual(self.request(filename="other.837").status_code, 409)
        self.assertEqual(AnalysisUpload.objects.count(), 2)

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

    def test_partial_file_saves_valid_claims_and_persists_rejections(self):
        for kind, segments in [
            ("837", ["CLM*GOODA*100", "CLM*BAD*wrong", "CLM*GOODB*.50"]),
            ("835", ["CLP*GOODA*1*100*80*20", "CLP*BAD*1*100*bad*0", "CLP*GOODB*1*.50*.25*.25"]),
        ]:
            with self.subTest(kind=kind):
                raw = envelope(kind, segments)
                response = self.request(kind, raw=raw, filename=f"partial.{kind}")
                self.assertEqual(response.status_code, 200, response.content)
                result = json.loads(response.content)["file"]
                self.assertEqual((result["status"], result["accepted_count"], result["rejected_count"]), ("PARTIAL", 2, 1))
                self.assertEqual([row["sequence"] for row in result["claims"]], [1, 3])
                self.assertEqual(result["rejections"][0]["sequence"], 2)
                upload = AnalysisUpload.objects.get(pk=result["id"])
                model = Analysis837Claim if kind == "837" else Analysis835Claim
                self.assertEqual(model.objects.filter(edi_file=upload).count(), 2)
                repeat = self.request(kind, raw=raw, filename=f"partial.{kind}")
                self.assertEqual(repeat.status_code, 409)
        self.assertEqual(AnalysisClaim.objects.count(), 4)
        self.assertEqual(AnalysisClaimRejection.objects.count(), 2)

    def test_normalized_837_columns_and_services_use_main_mapping(self):
        segments = ["HL*1**20*1", "NM1*85*2*EXAMPLE CLINIC*****XX*1234567890",
            "HL*2*1*22*0", "NM1*IL*1*DOE*JANE****MI*MEMBER123", "NM1*PR*2*EXAMPLE PAYER",
            "CLM*123456789ABC001*2483***11:B:1", "REF*9C*INTERNAL",
            "DTP*434*RD8*20261001-20261002", "HI*ABK:J00", "LX*1",
            "SV1*HC:99213:25*2483*UN*1***1", "DTP*472*D8*20261001"]
        response = self.request(raw=envelope(segments=segments))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["file"]["accepted_count"], 1)
        claim = Analysis837Claim.objects.get()
        self.assertEqual(claim.patient_first_name, "JANE")
        self.assertEqual(claim.patient_last_name, "DOE")
        self.assertEqual(claim.member_id, "MEMBER123")
        self.assertEqual(claim.billing_provider_name, "EXAMPLE CLINIC")
        self.assertEqual(claim.payer_name, "EXAMPLE PAYER")
        self.assertEqual(claim.diagnosis_codes, ["J00"])
        self.assertEqual(claim.service_from_date, "20261001")
        self.assertEqual(claim.total_charge_amount, Decimal("2483.00"))
        self.assertEqual(claim.service_count, 1)
        line = claim.service_lines.get()
        self.assertEqual(line.service_sequence, 1)
        self.assertEqual(line.procedure_code, "99213")
        self.assertEqual(line.modifiers, ["25"])
        self.assertEqual(line.charge_amount, Decimal("2483.00"))
        self.assertEqual(line.service_from_date, "20261001")
        self.assertIn("SV1", line.raw_segments)
        self.assertFalse(EDI837Claim.objects.exists())

    def test_blank_clp05_recovered_from_pr_and_survives_database_reload(self):
        raw = envelope("835", ["CLP*EXAMPLE*1*2483*284.77**ZZ*ID", "CAS*PR*1*10**2*.50",
                               "SVC*HC:99213*2483*284.77", "CAS*PR*3*20"])
        response = self.request("835", raw=raw)
        self.assertEqual(response.status_code, 200, response.content)
        stored = Analysis835Claim.objects.get()
        self.assertEqual(stored.patient_responsibility, Decimal("30.50"))
        self.assertEqual(stored.paid_amount, Decimal("284.77"))
        self.assertEqual(stored.service_count, 1)
        row = json.loads(self.get().content)["files"][0]["claims"][0]
        self.assertEqual(row["patient_responsibility"], "30.50")
        self.assertEqual(row["allowed"], "315.27")
        self.assertFalse(EDI835Claim.objects.exists())

    def test_bad_service_and_missing_identifier_reject_only_their_claim(self):
        raw = envelope(segments=["CLM*BAD*100", "LX*1", "SV1*HC:99213*oops*UN*1",
                                 "CLM**100", "CLM*GOOD*50"])
        result = json.loads(self.request(raw=raw).content)["file"]
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["rejected_count"], 2)
        self.assertEqual(result["claims"][0]["sequence"], 3)
        self.assertFalse(Analysis837ServiceLine.objects.exists())

    def test_all_invalid_claims_keep_diagnostics_without_comparison_rows(self):
        result = json.loads(self.request(raw=envelope(segments=["CLM*BAD*oops"])).content)["file"]
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(AnalysisClaim.objects.count(), 0)
        self.assertEqual(Analysis837Claim.objects.count(), 0)
        self.assertEqual(AnalysisUpload.objects.count(), 1)

    def test_structural_error_after_valid_claims_saves_nothing(self):
        raw = envelope(segments=["CLM*GOOD*100", "CLM*BAD*oops"])
        raw = raw.replace("SE*4*0001", "SE*9*0001")
        self.assertEqual(self.request(raw=raw).status_code, 400)
        self.assertFalse(AnalysisUpload.objects.exists())
        self.assertFalse(AnalysisClaim.objects.exists())

    def test_late_database_failure_rolls_back_all_normalized_tables(self):
        with patch("edi835.client_portal_analysis.Analysis837Claim.objects.bulk_create", side_effect=DatabaseError("failure")):
            self.assertEqual(self.request().status_code, 503)
        for model in (AnalysisUpload, AnalysisClaim, Analysis837Claim, Analysis837ServiceLine, AnalysisClaimRejection):
            self.assertEqual(model.objects.count(), 0)

    def test_legacy_backfill_is_scoped_and_idempotent(self):
        raw = envelope("835", ["CLP*EXAMPLE*1*2483*284.77**ZZ*ID"]).encode()
        upload = AnalysisUpload.objects.create(client=self.tenant, kind="835", filename="legacy.835",
            raw_content=raw, content_sha256=hashlib.sha256(raw).hexdigest())
        AnalysisClaim.objects.create(upload=upload, sequence=1, claim_number="EXAMPLE", internal_number="ID",
            billed="2483", payment="284.77", patient_responsibility=None)
        output = StringIO()
        call_command("reprocess_analysis_uploads", stdout=output)
        self.assertIn("Would process: 1 accepted", output.getvalue())
        self.assertIsNone(upload.claims.get().patient_responsibility)
        call_command("reprocess_analysis_uploads", apply=True, client_id=str(self.tenant.pk), stdout=StringIO())
        self.assertEqual(upload.claims.get().patient_responsibility, Decimal("0"))
        self.assertEqual(Analysis835Claim.objects.count(), 1)
        call_command("reprocess_analysis_uploads", apply=True, stdout=StringIO())
        self.assertEqual(Analysis835Claim.objects.count(), 1)
        row = json.loads(self.get().content)["files"][0]["claims"][0]
        self.assertEqual(row["allowed"], "284.77")

    def test_exact_duplicates_rejected_within_and_across_files(self):
        raw = envelope(segments=["CLM*EXACT*100", "DTP*434*D8*20261001", "CLM*EXACT*100", "DTP*434*D8*20261001"])
        first = json.loads(self.request(raw=raw, filename="first.837").content)["file"]
        self.assertEqual((first["accepted_count"], first["rejected_count"]), (1, 1))
        self.assertIn("exact duplicate", first["rejections"][0]["reason"])
        second = self.request(raw=raw, filename="second.837")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(AnalysisUpload.objects.count(), 1)
        self.assertEqual(AnalysisClaim.objects.count(), 1)
        self.assertEqual(Analysis837Claim.objects.count(), 1)

    def test_transport_control_changes_do_not_hide_exact_claim_duplicates(self):
        first = envelope(segments=["BHT*0019*00*FILE-A*20261001*1200*CH", "CLM*EXACT*100", "DTP*434*D8*20260901"])
        second = first.replace("FILE-A*20261001*1200", "FILE-B*20261002*1300").replace("000000001", "000000002")
        self.assertEqual(json.loads(self.request(raw=first, filename="a.837").content)["file"]["accepted_count"], 1)
        self.assertEqual(self.request(raw=second, filename="b.837").status_code, 409)
        self.assertEqual(AnalysisUpload.objects.count(), 1)

    def test_changed_dates_services_names_and_amounts_are_not_duplicates(self):
        base = ["HL*1**20*1", "NM1*85*2*CLINIC*****XX*1234567890", "HL*2*1*22*0", "NM1*IL*1*DOE*JANE****MI*MEMBER",
                "CLM*EXACT*100", "DTP*434*D8*20261001", "LX*1", "SV1*HC:99213*100*UN*1"]
        variations = [base, [value.replace("20261001", "20261002") for value in base],
            [value.replace("99213", "99214") for value in base], [value.replace("JANE", "JOHN") for value in base],
            [value.replace("*100", "*101") for value in base], [value.replace("CLINIC", "OTHER") for value in base],
            [value.replace("1234567890", "9876543210") for value in base]]
        for index, segments in enumerate(variations):
            result = json.loads(self.request(raw=envelope(segments=segments), filename=f"variation-{index}.837").content)["file"]
            self.assertEqual(result["accepted_count"], 1, result)
            self.assertEqual(result["rejected_count"], 0)
        self.assertEqual(Analysis837Claim.objects.count(), len(variations))

    def test_duplicate_scope_is_tenant_and_file_type(self):
        self.request(filename="first.837")
        other = Client.objects.create(name="Other", client_code="DEDUP-OTHER", email="dedup-other@example.com")
        self.user.client = other
        result = json.loads(self.request(filename="other.837").content)["file"]
        self.assertEqual(result["accepted_count"], 1)
        result = json.loads(self.request("835", raw=envelope("835", ["CLP*123456789*1*100*100*0"])).content)["file"]
        self.assertEqual(result["accepted_count"], 1)

    def test_835_duplicate_identity_includes_payment_dates_and_adjustments(self):
        base = ["BPR*I*80*C*CHK************20261001", "CLP*EXACT*1*100*80*20", "CAS*PR*1*20"]
        for index, segments in enumerate([base, base, [value.replace("20261001", "20261002") for value in base],
                                         [value.replace("CAS*PR*1", "CAS*PR*2") for value in base]]):
            response = self.request("835", raw=envelope("835", segments), filename=f"payer-{index}.835")
            if index == 1:
                self.assertEqual(response.status_code, 409)
            else:
                self.assertEqual(json.loads(response.content)["file"]["accepted_count"], 1)
        self.assertEqual(Analysis835Claim.objects.count(), 3)

    def test_mixed_duplicate_and_new_claims_save_only_new_claims(self):
        for kind, old, new in [("837", "CLM*OLD*100", "CLM*NEW*200"),
                               ("835", "CLP*OLD*1*100*80*20", "CLP*NEW*1*200*150*50")]:
            with self.subTest(kind=kind):
                name = f"original, unchanged.{kind}"
                self.assertEqual(self.request(kind, raw=envelope(kind, [old]), filename=name).status_code, 200)
                response = self.request(kind, raw=envelope(kind, [old, new]), filename=name)
                result = json.loads(response.content)["file"]
                self.assertEqual(result["filename"], name)
                self.assertEqual((result["accepted_count"], result["rejected_count"]), (1, 1))
                count = AnalysisUpload.objects.count()
                # Different bytes/name/order still cannot reinsert existing business claims.
                duplicate = envelope(kind, [new, old]).replace("000000001", "000000009")
                response = self.request(kind, raw=duplicate, filename=f"renamed.{kind}")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(AnalysisUpload.objects.count(), count)


class AnalysisIntakeRegressionTests(SimpleTestCase):
    def test_leading_decimal_formats_supported_without_rounding(self):
        for value, expected in [(".5", "0.50"), ("-.50", "-0.50"), ("0", "0.00")]:
            for kind, segment in [("837", f"CLM*A*{value}"), ("835", f"CLP*A*1*{value}*{value}*{value}")]:
                with self.subTest(kind=kind, value=value):
                    self.assertEqual(parse_analysis(envelope(kind, [segment]), kind)[0]["billed"], expected)
        for value in (".", "-.", "1.", "+.5", "1e2", "NaN", "1.234"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_analysis(envelope(segments=[f"CLM*A*{value}"]), "837")

    def test_multiple_interchanges_and_delimiters_preserve_sequences(self):
        raw = envelope("835", ["CLP*A*1*100*80*20"]) + "\n" + envelope("835", ["CLP*B*1*200*150*50"], "|", "!")
        rows = parse_analysis(raw, "835", details=True)
        self.assertEqual([row["sequence"] for row in rows], [1, 2])
        self.assertEqual([row["billed"] for row in rows], ["100.00", "200.00"])
        self.assertTrue(rows[1]["_normalized"]["raw_claim"].startswith("CLP|B|"))
        with self.assertRaises(ValueError):
            parse_analysis(raw[:-1], "835")

    def test_pr_adjustments_do_not_cross_claim_or_transaction_boundaries(self):
        raw = envelope("835", ["CLP*A*1*100*50**ZZ*ID", "CAS*PR*1*10", "CLP*B*1*100*20**ZZ*ID"])
        raw += envelope("835", ["CLP*C*1*-100*-50**ZZ*ID", "CAS*PR*1*-10", "CLP*D*1*100*50*40", "CAS*PR*1*40"])
        rows = parse_analysis(raw, "835")
        self.assertEqual([row["patient_responsibility"] for row in rows], ["10.00", "0.00", "-10.00", "40.00"])
        self.assertEqual([row["allowed"] for row in rows], ["60.00", "20.00", "-60.00", "90.00"])

    def test_invalid_pr_adjustment_never_becomes_zero(self):
        rejected = []
        rows = parse_analysis(envelope("835", ["CLP*BAD*1*100*50*", "CAS*PR*1*oops", "CLP*GOOD*1*100*50*50"]), "835", rejected=rejected, details=True)
        self.assertEqual([row["claim"] for row in rows], ["GOOD"])
        self.assertEqual(len(rejected), 1)
        self.assertIn("CAS PR", rejected[0]["reason"])

    def test_shared_837_parser_keeps_claim_reference_and_excludes_siblings(self):
        from .edi837_service import parse_837
        raw = envelope(segments=["CLM*FIRST*100", "REF*9C*CLAIM-REF", "LX*1", "REF*9C*SERVICE-REF",
                                 "CLM*SECOND*200"])
        claims = parse_837(raw)["claims"]
        self.assertEqual(claims[0]["reference_9c"], "CLAIM-REF")
        self.assertNotIn("CLM*FIRST", claims[1]["raw_claim"])
        self.assertEqual(claims[1]["raw_claim"].count("CLM*"), 1)

    def test_analysis_columns_match_main_intake_field_types(self):
        excluded = {"id", "edi_file", "client", "claim", "analysis_claim"}
        for main, staging in [(EDI835Claim, Analysis835Claim), (EDI837Claim, Analysis837Claim), (EDI837ServiceLine, Analysis837ServiceLine)]:
            main_fields = {field.name: field for field in main._meta.fields if field.name not in excluded}
            stage_fields = {field.name: field for field in staging._meta.fields if field.name not in excluded}
            self.assertEqual(set(main_fields), set(stage_fields))
            for name, field in main_fields.items():
                self.assertEqual(field.deconstruct()[1:], stage_fields[name].deconstruct()[1:])
