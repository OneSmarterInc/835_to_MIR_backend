from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from edi835.models import EDI835File, MIRFile
from edi835.tracked_files_eastern import _lightweight_payload


class ConversionDownloadSummaryTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Download Client",
            client_code="DOWNLOAD-CLIENT",
            email="ops@example.com",
        )
        self.user = User.objects.create_user(
            email="download@example.com",
            name="Download User",
            mobile="5550001000",
            password="test-password",
            client=self.client_record,
            is_active=True,
        )
        self.factory = RequestFactory()

    def test_canonical_mir_filename_keeps_download_action_available_without_output_path(self):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="source.835",
            stored_filename="source.835",
            status="ARCHIVED",
            output_path="",
            claims_count=1,
            delivered_claims_count=1,
        )
        MIRFile.objects.create(
            source_835=source,
            client=self.client_record,
            mir_filename="CANONICAL_DOWNLOAD.MIR",
            file_content="mir-content",
            file_hash="d" * 64,
            file_size=11,
            claim_count=1,
            physical_row_count=1,
            service_count=0,
            status="PUSHED",
        )

        request = self.factory.get("/edi835/api/tracked-files/")
        request.user = self.user

        payload = _lightweight_payload(request)

        self.assertEqual(len(payload["files"]), 1)
        item = payload["files"][0]
        self.assertEqual(item["mir_filename"], "CANONICAL_DOWNLOAD.MIR")
        self.assertEqual(item["output_path"], "CANONICAL_DOWNLOAD.MIR")
        self.assertEqual(item["status"], "ARCHIVED")
