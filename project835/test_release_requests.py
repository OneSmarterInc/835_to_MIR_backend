"""Positive mutation tests through the real middleware and CSRF-enabled routes."""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient, TestCase, override_settings

from accounts.models import Client, User
from edi835.models import AnalysisUpload, EDI835File
from edi835.test_client_portal_analysis import envelope


@override_settings(ROOT_URLCONF="project835.urls", MFA_ENFORCEMENT_ENABLED=False)
class ReleaseRequestTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Release test", client_code="RELEASE")
        self.user = User.objects.create_superuser(
            email="release@example.com", name="Release test", mobile="+15550001234",
            password="synthetic-test-password",
        )
        self.http = HttpClient(enforce_csrf_checks=True)
        self.http.force_login(self.user)
        self.token = self.http.get("/accounts/api/user/").json()["csrf_token"]

    def test_async_conversion_accepts_json_and_queues_without_transmitting(self):
        record = EDI835File.objects.create(
            client=self.tenant, original_filename="synthetic.835",
            input_file_content=envelope("835"),
        )
        with patch("converter.async_conversion.active_job_for", return_value=None), \
                patch("converter.async_conversion.write_job") as write:
            response = self.http.post(
                "/api/convert-async/", {"file_id": str(record.pk)},
                content_type="application/json", HTTP_X_CSRFTOKEN=self.token,
            )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(write.call_args.args[0]["file_id"], str(record.pk))
        self.assertEqual(response.json()["state"], "QUEUED")

    def test_admin_update_keeps_json_body_available_to_legacy_view(self):
        response = self.http.post(
            f"/accounts/api/admin/clients/{self.tenant.pk}/update/",
            {"name": "Updated release client"}, content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "Updated release client")

    def test_demo_analysis_multipart_saves_with_csrf_and_rejects_without_it(self):
        self.user.is_staff = False
        self.user.is_superuser = False
        self.user.client_role = "DEMO"
        self.user.client = self.tenant
        self.user.save()
        endpoint = "/edi835/api/client-portal/analysis/?compact=1"
        def upload():
            return {"kind": "837", "file": SimpleUploadedFile("extensionless", envelope().encode())}
        rejected = self.http.post(endpoint, upload())
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["code"], "CSRF_FAILED")
        self.assertFalse(AnalysisUpload.objects.exists())
        response = self.http.post(endpoint, upload(), HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AnalysisUpload.objects.get().client_id, self.tenant.pk)

    def test_untrusted_origin_is_rejected_even_with_valid_token(self):
        response = self.http.post(
            "/accounts/api/logout/", {}, content_type="application/json",
            HTTP_X_CSRFTOKEN=self.token, HTTP_ORIGIN="https://untrusted.example",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "CSRF_FAILED")
