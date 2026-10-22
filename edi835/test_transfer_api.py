import io
import json
import uuid
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.db import DatabaseError
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from . import test_intake as fixtures, test_intake_recovery as recovery
from .batch_jobs import claim_job, write_job
from .intake_lifecycle import begin_effects, execution
from .intake_storage import source_path
from .models import BatchDispatch, InboundTransfer, ProcessingAttempt, SubmissionKey, TransferRetry
from .transfer_service import SubmissionPending


@override_settings(ROOT_URLCONF="project835.urls", INTAKE_PARTNER_ENABLED=True, MFA_ENFORCEMENT_ENABLED=False)
class TransferAPITests(TestCase):
    setUp = fixtures.IntakeTests.setUp
    register = fixtures.IntakeTests.register
    output = recovery.IntakeRecoveryTests.output

    def submit(self, raw=b"synthetic", key="submission-key", **kwargs):
        data = {"workflow": "837", "file": SimpleUploadedFile("original", raw)}
        data.update(kwargs.pop("data", {}))
        return self.client.post("/api/v1/transfers", data, secure=True,
            HTTP_AUTHORIZATION=self.authorization, HTTP_IDEMPOTENCY_KEY=key, **kwargs)

    def partner_get(self, path, **kwargs):
        return self.client.get(path, secure=True, HTTP_AUTHORIZATION=self.authorization, **kwargs)

    def fail(self, receipt, effects=False):
        job = claim_job(str(receipt.dispatch_id))
        if effects:
            with execution(job):
                begin_effects()
        job.update(state="FAILED", result={"success": False})
        write_job(job)
        receipt.refresh_from_db()
        return job

    def retry(self, receipt, reason="Upstream dependency restored"):
        return self.client.post(f"/api/v1/transfers/{receipt.pk}/retry",
            json.dumps({"reason": reason}), content_type="application/json")

    def test_receipt_is_durable_before_202_and_recovers_after_lost_response(self):
        first = self.submit()
        self.assertEqual(first.status_code, 202, first.content)
        body = first.json()
        receipt = InboundTransfer.objects.get(pk=body["receipt_id"])
        self.assertTrue(source_path(receipt.storage_key).exists())
        self.assertEqual(receipt.dispatch.state, "QUEUED")
        self.assertEqual(first["Location"], body["status_url"])
        recover = self.partner_get("/api/v1/transfers/recover", HTTP_IDEMPOTENCY_KEY="submission-key")
        self.assertEqual(recover.json()["receipt_id"], body["receipt_id"])
        replay = self.submit()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["receipt_id"], body["receipt_id"])
        self.assertEqual(SubmissionKey.objects.count(), 1)
        self.assertEqual(BatchDispatch.objects.count(), 1)
        self.assertEqual(len(list(source_path(receipt.storage_key).parent.glob("*.bin"))), 1)

    def test_key_conflict_for_changed_bytes_or_workflow(self):
        self.submit()
        self.assertEqual(self.submit(raw=b"changed").status_code, 409)
        self.partner.workflows = ["835", "837"]
        self.partner.save()
        conflict = self.submit(data={"workflow": "835"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(InboundTransfer.objects.count(), 1)

    def test_staff_key_cannot_be_reused_for_another_authorized_client(self):
        self.client.force_login(self.admin)
        def send(client):
            return self.client.post("/api/v1/transfers", {"client_id": str(client.pk), "workflow": "837",
                "file": SimpleUploadedFile("source", b"same")}, HTTP_IDEMPOTENCY_KEY="same-key")
        self.assertEqual(send(self.tenant).status_code, 202)
        self.assertEqual(send(self.other).status_code, 409)

    def test_new_key_duplicate_has_its_own_linked_receipt(self):
        first = self.submit().json()
        second = self.submit(key="another")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["state"], "DUPLICATE")
        self.assertEqual(second.json()["canonical_receipt_id"], first["receipt_id"])
        self.assertEqual(BatchDispatch.objects.filter(state="QUEUED").count(), 1)

    def test_transaction_failure_does_not_acknowledge_or_reserve_key(self):
        with patch.object(SubmissionKey.objects, "create", side_effect=DatabaseError("synthetic")):
            result = self.submit()
        self.assertEqual(result.status_code, 503)
        self.assertFalse(InboundTransfer.objects.exists())
        self.assertFalse(BatchDispatch.objects.exists())
        self.assertFalse(SubmissionKey.objects.exists())
        self.assertEqual(self.submit().status_code, 202)

    def test_unresolved_submission_has_safe_retry_after(self):
        with patch("edi835.transfer_api.submit", side_effect=SubmissionPending("Still committing")):
            response = self.submit()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Retry-After"], "2")
        self.assertEqual(response.json()["code"], "SUBMISSION_IN_PROGRESS")

    def test_partner_cross_client_endpoint_matrix(self):
        receipt = self.register(client=self.other)
        path = f"/api/v1/transfers/{receipt.pk}"
        for suffix in ("", "/source", f"/outputs/{uuid.uuid4()}"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.partner_get(path + suffix).status_code, 404)
        self.assertEqual(self.partner_get(f"/api/v1/transfers?client_id={self.other.pk}").status_code, 404)
        self.assertEqual(self.partner_get("/api/v1/transfers").json()["total"], 0)
        self.assertEqual(self.submit(data={"client_id": str(self.other.pk)}).status_code, 404)
        response = self.client.post(path + "/retry", {"reason": "repair"}, secure=True, HTTP_AUTHORIZATION=self.authorization)
        self.assertEqual(response.status_code, 404)

    def test_staff_cross_client_endpoint_matrix(self):
        self.admin.is_superuser = False
        self.admin.client = self.tenant
        self.admin.save()
        self.client.force_login(self.admin)
        receipt = self.register(client=self.other)
        path = f"/api/v1/transfers/{receipt.pk}"
        for suffix in ("", "/source", f"/outputs/{uuid.uuid4()}"):
            self.assertEqual(self.client.get(path + suffix).status_code, 404)
        self.assertEqual(self.retry(receipt).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/transfers").json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/transfers?client_id={self.other.pk}").status_code, 404)
        response = self.client.post("/api/v1/transfers", {"client_id": str(self.other.pk), "workflow": "837",
            "file": SimpleUploadedFile("original", b"source")}, HTTP_IDEMPOTENCY_KEY="tenant-denial")
        self.assertEqual(response.status_code, 404)

    def test_partner_cannot_retry_even_own_failed_transfer(self):
        receipt = InboundTransfer.objects.get(pk=self.submit().json()["receipt_id"])
        self.fail(receipt)
        response = self.client.post(f"/api/v1/transfers/{receipt.pk}/retry", json.dumps({"reason": "repair"}),
            content_type="application/json", secure=True, HTTP_AUTHORIZATION=self.authorization)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TransferRetry.objects.exists())

    def test_history_filters_and_stable_pagination(self):
        self.client.force_login(self.admin)
        a = self.register(raw=b"a", channel="SFTP")
        self.register(raw=b"b", channel="STAFF")
        InboundTransfer.objects.filter(pk=a.pk).update(received_at=timezone.now() - timedelta(days=3))
        base = f"/api/v1/transfers?client_id={self.tenant.pk}"
        self.assertEqual(self.client.get(base + "&page_size=1").json()["pages"], 2)
        first = self.client.get(base + "&page_size=1").json()["transfers"][0]["receipt_id"]
        second = self.client.get(base + "&page_size=1&page=2").json()["transfers"][0]["receipt_id"]
        self.assertNotEqual(first, second)
        response = self.client.get(base + "&channel=SFTP&status=QUEUED")
        self.assertEqual(response.json()["transfers"][0]["receipt_id"], str(a.pk))
        today = timezone.now().date().isoformat()
        self.assertEqual(self.client.get(base + f"&date_from={today}&date_to={today}").json()["total"], 1)
        for value in ("status=UNKNOWN", "channel=INVALID", "date_from=broken", "date_from=2026-02-30", "page=-1", "page_size=101", "page=99"):
            self.assertEqual(self.client.get(base + "&" + value).status_code, 400, value)

    def test_downloads_require_auth_and_do_not_expose_storage_paths(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        output, text, _ = self.output(receipt, job)
        url = f"/api/v1/transfers/{receipt.pk}"
        self.assertEqual(self.client.get(url + "/source").status_code, 401)
        self.client.force_login(self.admin)
        detail = self.client.get(url).json()
        self.assertNotIn("storage_key", json.dumps(detail))
        for path, content in ((detail["source_url"], b"source"), (detail["outputs"][0]["download_url"], text.encode())):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), content)
            response.close()
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertIn("attachment", response["Content-Disposition"])
        source_path(output.storage_key).write_bytes(b"tampered")
        self.assertEqual(self.client.get(detail["outputs"][0]["download_url"]).status_code, 503)

    def test_foreign_output_id_denied_on_own_receipt(self):
        a = self.register(workflow="835")
        output, _, _ = self.output(a, claim_job(str(a.dispatch_id)))
        b = self.register(raw=b"another", client=self.other)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(f"/api/v1/transfers/{b.pk}/outputs/{output.pk}").status_code, 404)

    def test_safe_retry_is_audited_and_links_new_attempt_to_parent(self):
        self.client.force_login(self.admin)
        receipt = self.register()
        previous = self.fail(receipt)
        response = self.retry(receipt)
        self.assertEqual(response.status_code, 202, response.content)
        audit = TransferRetry.objects.get()
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(str(audit.parent_id), previous["attempt_id"])
        self.assertEqual(audit.reason, "Upstream dependency restored")
        self.assertEqual(self.retry(receipt).status_code, 409)
        next_job = claim_job(str(receipt.dispatch_id))
        audit.refresh_from_db()
        self.assertEqual(str(audit.attempt_id), next_job["attempt_id"])
        self.assertEqual(audit.attempt.parent_id, audit.parent_id)
        self.assertEqual(audit.attempt.retry_reason, "OPERATOR_RETRY")

    def test_retry_denies_effects_invalid_state_and_attempt_limit(self):
        self.client.force_login(self.admin)
        receipt = self.register()
        self.assertEqual(self.retry(receipt).status_code, 409)
        self.fail(receipt, effects=True)
        self.assertEqual(self.retry(receipt).status_code, 409)
        other = self.register(raw=b"safe")
        self.fail(other)
        with override_settings(INTAKE_MAX_ATTEMPTS=1):
            self.assertEqual(self.retry(other).status_code, 409)
        self.assertEqual(self.retry(other, reason="").status_code, 400)
        source_path(other.storage_key).unlink()
        self.assertEqual(self.retry(other).status_code, 503)
        self.assertFalse(TransferRetry.objects.exists())

    def test_sessions_csrf_mfa_and_expired_credentials(self):
        strict = HttpClient(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post("/api/v1/transfers", {}).status_code, 403)
        receipt = self.register()
        self.fail(receipt)
        self.assertEqual(strict.post(f"/api/v1/transfers/{receipt.pk}/retry", {}).status_code, 403)
        with override_settings(MFA_ENFORCEMENT_ENABLED=True):
            self.assertEqual(strict.get("/api/v1/transfers").status_code, 403)
        self.partner.expires_at = timezone.now() - timedelta(seconds=1)
        self.partner.save()
        self.assertEqual(self.submit().status_code, 401)
        self.assertEqual(strict.get("/api/v1/transfers", HTTP_AUTHORIZATION="Bearer bad", secure=True).status_code, 401)
        strict.logout()
        self.assertEqual(strict.get("/api/v1/transfers").status_code, 401)

    def test_staff_receipt_survives_session_expiry_and_reauthentication(self):
        self.client.force_login(self.admin)
        response = self.client.post("/api/v1/transfers", {"client_id": str(self.tenant.pk), "workflow": "837",
            "file": SimpleUploadedFile("source", b"same")}, HTTP_IDEMPOTENCY_KEY="recover-staff")
        self.assertEqual(response.status_code, 202)
        self.client.logout()
        path = "/api/v1/transfers/recover"
        self.assertEqual(self.client.get(path, HTTP_IDEMPOTENCY_KEY="recover-staff").status_code, 401)
        self.client.force_login(self.admin)
        recovered = self.client.get(path, HTTP_IDEMPOTENCY_KEY="recover-staff")
        self.assertEqual(recovered.json()["receipt_id"], response.json()["receipt_id"])

    def test_malformed_oversized_and_executable_uploads(self):
        for raw in (b"MZbad", b"PK\x03\x04archive", b""):
            self.assertEqual(self.submit(raw=raw).status_code, 400)
        with override_settings(INTAKE_MAX_BYTES=3):
            self.assertEqual(self.submit(raw=b"1234").status_code, 413)
        with override_settings(INTAKE_MAX_REQUEST_BYTES=1):
            self.assertEqual(self.submit().status_code, 413)
        self.assertEqual(self.submit(data={"workflow": "bad"}).status_code, 400)
        self.assertEqual(self.submit(key="").status_code, 400)
        self.assertFalse(InboundTransfer.objects.exists())

    def test_no_public_media_mount(self):
        self.assertEqual(self.client.get("/media/synthetic.835").status_code, 404)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://portal.example.test"])
    def test_cross_origin_recovery_header_is_allowed(self):
        response = self.client.options("/api/v1/transfers/recover", HTTP_ORIGIN="https://portal.example.test",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET", HTTP_ACCESS_CONTROL_REQUEST_HEADERS="idempotency-key")
        self.assertEqual(response.status_code, 200)
        self.assertIn("idempotency-key", response["Access-Control-Allow-Headers"])
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://portal.example.test")

    def test_history_does_not_fetch_dispatch_payloads_or_query_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_login(self.admin)
        for number in range(12):
            self.register(raw=str(number).encode())
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/transfers?page_size=12")
        self.assertEqual(response.json()["total"], 12)
        self.assertLess(len(queries), 8)
        self.assertFalse(any('"payload"' in item["sql"] for item in queries))

    def test_python_client_streams_multipart_through_real_api_and_worker(self):
        from examples.transfer_client import PartnerClient
        from .management.commands.run_batch_worker import Command as Worker
        from .models import EDI837File
        http = self.client
        connections = []

        class Connection:
            def __init__(self, *args, **kwargs):
                self.headers, self.chunks = {}, []
                connections.append(self)

            def putrequest(self, method, path):
                self.method, self.path = method, path

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self, prefix):
                self.chunks.append(prefix)

            def send(self, chunk):
                self.chunks.append(chunk)

            def request(self, method, path, headers):
                self.method, self.path, self.headers = method, path, headers

            def getresponse(self):
                headers = {"HTTP_AUTHORIZATION": self.headers["Authorization"]}
                if self.headers.get("Idempotency-Key"):
                    headers["HTTP_IDEMPOTENCY_KEY"] = self.headers["Idempotency-Key"]
                if self.method == "POST":
                    body = b"".join(self.chunks)
                    assert len(body) == int(self.headers["Content-Length"])
                    response = http.post(self.path, body, content_type=self.headers["Content-Type"], secure=True, **headers)
                else:
                    response = http.get(self.path, secure=True, **headers)
                result = io.BytesIO(response.content)
                result.status = response.status_code
                return result

            def close(self):
                pass

        client = PartnerClient("https://example.test", self.authorization[7:])
        sample = Path(settings.BASE_DIR) / "examples/synthetic_claim.837"
        with patch("examples.transfer_client.http.client.HTTPSConnection", Connection):
            status, response = client.request("/api/v1/transfers", file=sample, workflow="837", key="client-key")
            self.assertEqual(status, 202, response)
            receipt = InboundTransfer.objects.get(pk=response["receipt_id"])
            Worker()._process(receipt.dispatch.payload)
            status, result = client.request(response["status_url"])
            self.assertEqual(status, 200)
            self.assertEqual(result["state"], "COMPLETED", result)
            self.assertEqual(EDI837File.objects.count(), 1)
            status, recovered = client.request("/api/v1/transfers/recover", key="client-key")
            self.assertEqual(status, 200)
            self.assertEqual(recovered["receipt_id"], response["receipt_id"])
            self.assertTrue(all(len(chunk) <= 1024 * 1024 for conn in connections for chunk in conn.chunks))

    def test_published_schema_and_synthetic_fixtures(self):
        from .client_portal_analysis import parse_analysis
        response = self.client.get("/api/v1/transfers/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = json.loads(b"".join(response.streaming_content))
        response.close()
        self.assertEqual(schema["openapi"], "3.0.3")
        self.assertIn("/api/v1/transfers/{receipt_id}/retry", schema["paths"])
        for filename, kind in (("synthetic_claim.837", "837"), ("synthetic_payment.835", "835")):
            raw = (Path(settings.BASE_DIR) / "examples" / filename).read_bytes()
            self.assertEqual(len(parse_analysis(raw.decode(), kind)), 1)
