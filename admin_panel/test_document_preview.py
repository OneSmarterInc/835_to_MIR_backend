import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import Client, User
from admin_panel.models import ClientDocument


class DocumentPreviewTestCase(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.admin = User.objects.create_superuser(
            email="preview-admin@example.com",
            name="Preview Admin",
            mobile="5550199800",
            password="test-password",
        )
        client = Client.objects.create(
            name="Preview Client",
            client_code="PREVIEW",
            email="preview@example.com",
        )
        self.document = ClientDocument.objects.create(
            client=client,
            document_name="Example PDF",
            original_filename="example.pdf",
            document_type="General Document",
            file=SimpleUploadedFile("example.pdf", b"%PDF-1.4\npreview-content", content_type="application/pdf"),
            file_size=24,
        )

    def test_signed_preview_streams_file_without_loading_it_into_json(self):
        self.client.force_login(self.admin)
        metadata = self.client.get(
            f"/admin-panel/api/documents/{self.document.id}/preview-url/"
        )
        self.assertEqual(metadata.status_code, 200)
        preview_url = metadata.json()["preview_url"]

        preview = self.client.get(preview_url)

        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.streaming)
        self.assertEqual(preview["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(preview.streaming_content), b"%PDF-1.4\npreview-content")

    def test_preview_rejects_invalid_signature(self):
        response = self.client.get(
            f"/admin-panel/api/documents/{self.document.id}/preview/?token=invalid"
        )
        self.assertEqual(response.status_code, 403)
