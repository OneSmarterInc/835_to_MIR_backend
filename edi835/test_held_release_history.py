import json
from types import SimpleNamespace

from django.test import RequestFactory, TestCase

from accounts.models import Client
from .held_release_views import api_held_release_history
from .models import EDI835File, MIRClaim, MIRFile


class HeldReleaseHistoryTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Held Release Test",
            client_code="HELD-REL-TEST",
            email="held-release@example.com",
        )
        self.source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="source.835",
            stored_filename="source.835",
            held_claims_count=0,
            conversion_findings=[
                {
                    "rule_code": "DUPLICATE_RECENT_MIR",
                    "claim_number": "12345678901234567",
                    "previous_mir_filename": "MIROUT_PREVIOUS.MIR",
                    "previous_mir_id": "previous-id",
                    "previous_sent_at": "2026-09-11T10:10:21+00:00",
                    "eligible_send_at": "2026-09-14T21:30:00+00:00",
                    "release_status": "SENT",
                    "released_at": "2026-09-14T21:30:10+00:00",
                    "release_mir_filename": "202609141710.MIR",
                }
            ],
        )
        self.release = EDI835File.objects.create(
            client=self.client_record,
            original_filename="held_release_202609141710.835",
            stored_filename="held_release_202609141710.835",
            ingestion_source="HELD_RELEASE",
            status="ARCHIVED",
            delivered_claims_count=1,
            services_count=2,
            present_in_sftp=True,
        )
        mir = MIRFile.objects.create(
            source_835=self.release,
            client=self.client_record,
            mir_filename="202609141710.MIR",
            file_content="test",
            file_hash="a" * 64,
            claim_count=1,
            physical_row_count=1,
            service_count=2,
            status="PUSHED",
        )
        MIRClaim.objects.create(
            mir_file=mir,
            claim_sequence=1,
            claim_control_number="12345678901234567ABC123",
            service_count=2,
            header_raw=" " * 334,
        )

    def test_release_history_returns_sftp_status_and_hold_provenance(self):
        request = RequestFactory().get("/edi835/api/checks/held-releases/")
        request.user = SimpleNamespace(
            is_staff=False,
            is_superuser=False,
            client=self.client_record,
        )

        response = api_held_release_history(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))

        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["releases"]), 1)
        release = payload["releases"][0]
        self.assertEqual(release["mir_filename"], "202609141710.MIR")
        self.assertEqual(release["sftp_status"], "PUSHED")
        self.assertTrue(release["pushed"])
        self.assertEqual(release["claim_count"], 1)
        self.assertEqual(release["claims"][0]["claim_number"], "12345678901234567")
        self.assertEqual(release["claims"][0]["held_from_mir"], "MIROUT_PREVIOUS.MIR")
        self.assertEqual(release["claims"][0]["source_835_filename"], "source.835")
