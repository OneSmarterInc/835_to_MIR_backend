"""Client User may operate shared conversion UI only within its assigned tenant."""
import json
from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from accounts.models import Client, User
from edi835.models import EDI835File
from project835.middleware import AdminAccessMiddleware


@override_settings(MFA_ENFORCEMENT_ENABLED=False)
class ClientUserConversionAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.a = Client.objects.create(name="A", client_code="CU-A-CV", email="a@example.org")
        self.b = Client.objects.create(name="B", client_code="CU-B-CV", email="b@example.org")
        self.viewer = User.objects.create_user(
            email="view-cv@example.org", name="Viewer", mobile="+15552110110",
            password="TestPassword123!", client=self.a, client_role="CLIENT_USER",
        )
        self.own = EDI835File.objects.create(
            client=self.a, original_filename="own.835", stored_filename="own.835",
        )
        self.other = EDI835File.objects.create(
            client=self.b, original_filename="other.835", stored_filename="other.835",
        )
        self.middleware = AdminAccessMiddleware(
            lambda request: JsonResponse({"success": True, "allowed": True})
        )

    def post(self, path, data, confirmed=False):
        headers = {"HTTP_X_FILEX_CONFIRM_SEND": "yes"} if confirmed else {}
        request = self.factory.post(
            path, data=json.dumps(data), content_type="application/json", **headers
        )
        request.user = self.viewer
        return self.middleware(request)

    def test_validate_own_tenant_is_allowed(self):
        result = self.post("/api/validate/", {"client_id": str(self.a.pk)})
        self.assertEqual(result.status_code, 200, result.content)

    def test_client_ui_reads_are_allowed(self):
        paths = [
            "/edi835/api/client-portal/overview/",
            "/edi835/api/client-portal/comparison/",
            "/edi835/api/client-portal/comparison-835-837/",
            "/edi835/api/client-portal/history/",
            "/edi835/api/client-portal/actions/",
            "/edi835/api/client-portal/analysis/",
            "/edi835/api/client-portal/claim/1/",
            "/edi835/api/client-portal/claim/1/source/835/",
            "/edi835/api/client-portal/claim/1/download/",
            "/edi835/api/tracked-files/",
            f"/api/file-content/{self.own.pk}/",
            "/api/start-batch-conversion/?job_id=test",
            "/accounts/api/webauthn/credentials/",
        ]
        for path in paths:
            with self.subTest(path=path):
                request = self.factory.get(path)
                request.user = self.viewer
                self.assertEqual(self.middleware(request).status_code, 200)

    def test_client_ui_writes_allowed_without_admin_operations(self):
        for path in (
            "/edi835/api/client-portal/analysis/",
            "/edi835/api/client-portal/actions/",
            "/edi835/api/client-portal/convert/",
            "/accounts/api/webauthn/register/options/",
            "/accounts/api/webauthn/register/verify/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}).status_code, 200)
        for path in (
            "/admin-panel/api/users/create/",
            "/edi835/api/client-portal/actions/1/review/",
            "/edi835/api/sftp/save/",
            "/edi835/api/837/upload-process/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}).status_code, 403)

    def test_validate_other_tenant_is_denied(self):
        result = self.post("/api/validate/", {"client_id": str(self.b.pk)})
        self.assertEqual(result.status_code, 403)

    def test_process_requires_delivery_confirmation(self):
        result = self.post("/api/convert/", {"file_id": str(self.own.pk)})
        self.assertEqual(result.status_code, 400)

    def test_process_own_file_with_confirmation_is_allowed(self):
        result = self.post("/api/convert/", {"file_id": str(self.own.pk)}, confirmed=True)
        self.assertEqual(result.status_code, 200, result.content)

    def test_processing_foreign_tenant_file_is_denied(self):
        result = self.post("/api/convert/", {"file_id": str(self.other.pk)}, confirmed=True)
        self.assertEqual(result.status_code, 404)

    def test_batch_requires_835_only(self):
        result = self.post("/api/start-batch-conversion/", {"automation_type": "ALL"}, confirmed=True)
        self.assertEqual(result.status_code, 403)
        allowed = self.post("/api/start-batch-conversion/", {"automation_type": "835"}, confirmed=True)
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_download_requires_file_id_and_tenant(self):
        request = self.factory.get("/api/download/")
        request.user = self.viewer
        self.assertEqual(self.middleware(request).status_code, 400)
        request = self.factory.get("/api/download/?file_id=" + str(self.other.pk))
        request.user = self.viewer
        self.assertEqual(self.middleware(request).status_code, 404)
        request = self.factory.get("/api/download/?file_id=" + str(self.own.pk))
        request.user = self.viewer
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_force_push_forbidden(self):
        result = self.post("/edi835/api/sftp/push/", {"file_id": str(self.own.pk), "force": True}, confirmed=True)
        self.assertEqual(result.status_code, 403)
