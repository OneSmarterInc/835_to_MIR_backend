from unittest.mock import patch

from django.test import TestCase

from accounts.models import Client, User
from edi835.held_release_email import send_held_release_sftp_notice
from edi835.mir_persistence import set_mir_push_status
from edi835.models import EDI835File, MIRClaim, MIRFile


class HeldReleaseEmailTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Held Email Client",
            client_code="HELD-EMAIL",
            email="primary@example.com",
        )
        self.user = User.objects.create_user(
            email="portal@example.com",
            name="Portal User",
            mobile="+15550001001",
            password="test-password",
            client=self.client_record,
        )
        self.original_source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="source.835",
            stored_filename="source.835",
            status="ARCHIVED",
            conversion_findings=[
                {
                    "rule_code": "DUPLICATE_RECENT_MIR",
                    "severity": "INFO",
                    "release_status": "SENT",
                    "claim_index": "1",
                    "claim_number": "12345678901234567",
                    "previous_mir_filename": "MIROUT_PREVIOUS.MIR",
                    "previous_sent_at": "2026-09-11T10:10:21+00:00",
                    "eligible_send_at": "2026-09-14T21:30:00+00:00",
                    "released_at": "2026-09-14T21:30:10+00:00",
                    "release_mir_filename": "202609141700.MIR",
                },
                {
                    "rule_code": "DUPLICATE_RECENT_MIR",
                    "severity": "INFO",
                    "release_status": "SENT",
                    "claim_index": "2",
                    "claim_number": "76543210987654321",
                    "previous_mir_filename": "MIROUT_PREVIOUS_2.MIR",
                    "previous_sent_at": "2026-09-11T11:10:21+00:00",
                    "eligible_send_at": "2026-09-14T21:30:00+00:00",
                    "released_at": "2026-09-14T21:30:10+00:00",
                    "release_mir_filename": "202609141700.MIR",
                },
            ],
        )
        self.release_source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="held_release_202609141700.835",
            stored_filename="held_release_202609141700.835",
            ingestion_source="HELD_RELEASE",
            status="ARCHIVED",
            delivered_claims_count=2,
            services_count=5,
            present_in_sftp=True,
        )
        self.mir = MIRFile.objects.create(
            source_835=self.release_source,
            client=self.client_record,
            mir_filename="202609141700.MIR",
            file_content="test",
            file_hash="a" * 64,
            file_size=4,
            claim_count=2,
            physical_row_count=2,
            service_count=5,
            status="PUSHED",
        )
        MIRClaim.objects.create(
            mir_file=self.mir,
            claim_sequence=1,
            claim_control_number="12345678901234567ABC123",
            service_count=2,
            header_raw=" " * 334,
        )
        MIRClaim.objects.create(
            mir_file=self.mir,
            claim_sequence=2,
            claim_control_number="76543210987654321XYZ987",
            service_count=3,
            header_raw=" " * 334,
        )

    @patch("edi835.held_release_email.send_client_email", return_value=True)
    def test_email_lists_every_released_claim_and_provenance(self, send_email):
        sent = send_held_release_sftp_notice(self.mir)

        self.assertTrue(sent)
        send_email.assert_called_once()
        client, subject, html = send_email.call_args.args
        recipients = send_email.call_args.kwargs["to_emails"]

        self.assertEqual(client, self.client_record)
        self.assertIn("202609141700.MIR", subject)
        self.assertIn("12345678901234567", html)
        self.assertIn("76543210987654321", html)
        self.assertIn("MIROUT_PREVIOUS.MIR", html)
        self.assertIn("MIROUT_PREVIOUS_2.MIR", html)
        self.assertIn("source.835", html)
        self.assertIn("Claims sent", html)
        self.assertIn("Service lines sent", html)
        self.assertEqual(set(recipients), {"primary@example.com", "portal@example.com"})

    @patch("edi835.held_release_email.send_held_release_sftp_notice", return_value=False)
    @patch("edi835.held_claims.note_mir_sent")
    def test_email_failure_never_reverses_successful_sftp_status(self, note_sent, send_notice):
        self.mir.status = "GENERATED"
        self.mir.save(update_fields=["status"])

        set_mir_push_status(self.mir, True)
        self.mir.refresh_from_db()

        self.assertEqual(self.mir.status, "PUSHED")
        note_sent.assert_called_once()
        send_notice.assert_called_once()

    @patch("edi835.held_release_email.send_held_release_sftp_notice")
    @patch("edi835.held_claims.note_mir_sent")
    def test_normal_mir_push_does_not_send_held_release_email(self, note_sent, send_notice):
        normal_source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="normal.835",
            stored_filename="normal.835",
            ingestion_source="MANUAL",
            status="ARCHIVED",
        )
        normal_mir = MIRFile.objects.create(
            source_835=normal_source,
            client=self.client_record,
            mir_filename="NORMAL.MIR",
            file_content="test",
            file_hash="b" * 64,
            file_size=4,
            claim_count=0,
            physical_row_count=0,
            service_count=0,
            status="GENERATED",
        )

        set_mir_push_status(normal_mir, True)

        note_sent.assert_called_once()
        send_notice.assert_not_called()
