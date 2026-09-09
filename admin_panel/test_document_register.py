import shutil
import tempfile

from django.test import TestCase, override_settings

from accounts.models import Client, User
from admin_panel.models import ClientDocument


@override_settings(MFA_ENFORCEMENT_ENABLED=False)
class DocumentRegisterTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="document-register-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root, SECURE_SSL_REDIRECT=False)
        self.settings_override.enable()
        self.admin = User.objects.create_superuser(
            email="document-admin@example.com", password="test-password", name="Document Admin",
            mobile="5550009001",
        )
        self.tenant = Client.objects.create(
            name="Document Tenant", client_code="DOCS", email="documents@example.com"
        )
        self.client.force_login(self.admin)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_register_includes_untouched_workflow_documents(self):
        response = self.client.get(f"/admin-panel/api/clients/{self.tenant.id}/documents/")

        self.assertEqual(response.status_code, 200)
        documents = response.json()["documents"]
        nda = next(row for row in documents if row["document_type"] == "Onboarding Step 1")
        self.assertIsNone(nda["id"])
        self.assertIsNone(nda["version"])
        self.assertEqual(nda["state"], "NOT RECEIVED")
        self.assertEqual(nda["direction"], "Both")

    def test_failed_validation_is_saved_and_increments_version(self):
        url = f"/admin-panel/api/clients/{self.tenant.id}/documents/upload/"
        headers = {
            "HTTP_X_FILENAME": "signed-nda.pdf",
            "HTTP_X_DOC_NAME": "Mutual NDA",
            "HTTP_X_DOC_TYPE": "Onboarding%20Step%201",
            "HTTP_X_EXPIRATION_DATE": "2028-08-04",
        }

        first = self.client.post(url, data=b"not a valid PDF", content_type="application/pdf", **headers)
        second = self.client.post(url, data=b"still not a valid PDF", content_type="application/pdf", **headers)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["validation_ok"])
        self.assertEqual(first.json()["version"], 1)
        self.assertEqual(second.json()["version"], 2)
        versions = list(ClientDocument.objects.filter(client=self.tenant).order_by("version"))
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertTrue(all(item.validation_status == "INVALID" for item in versions))
        self.assertEqual(str(versions[-1].expiration_date), "2028-08-04")
