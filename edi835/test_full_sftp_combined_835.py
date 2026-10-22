import io
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from accounts.models import Client

from .full_sftp_pipeline import process_835_to_mir_sftp
from .test_sftp_intake import FakeSFTP, CREDENTIALS, age_observations


class _FakeSSH:
    def close(self):
        pass


_FakeSFTP = FakeSFTP


class CombinedSFTP835ContractTests(TestCase):
    def setUp(self):
        self.client = Client.objects.create(name="Synthetic", client_code="SFTP-COMBINED")
        self.actor = SimpleNamespace(is_authenticated=True)
        self.credentials = {**CREDENTIALS, "remote_folder": "/in/835"}

    @patch("edi835.full_sftp_pipeline.process_multiple_edi835_files")
    @patch("edi835.full_sftp_pipeline._normalize_folder", return_value="/in/835")
    @patch("edi835.full_sftp_pipeline.resolve_admin_sftp_route")
    @patch("edi835.full_sftp_pipeline._open_sftp")
    def test_multiple_inbound_835_files_create_one_combined_mir_then_delete_sources(
        self, open_sftp, resolve_route, _normalize, process_multiple
    ):
        sftp = _FakeSFTP({
            "first.835": b"ISA*FIRST~CLP*ONE~",
            "second.x12": b"ISA*SECOND~CLP*TWO~",
        })
        open_sftp.return_value = (_FakeSSH(), sftp)
        resolve_route.return_value = (SimpleNamespace(), self.credentials, "/in/835")
        process_multiple.return_value = {
            "success": True,
            "accepted_files": ["first.835", "second.x12"],
            "refused_files": [],
            "combined_filename": "MIROUT_COMBINED.MIR",
            "sftp_uploaded": True,
            "errors": [],
        }

        def accept(items, **kwargs):
            # Model the converter's durable acceptance/output contract.
            from .intake_service import register_sources
            from .models import EDI835File, IntakeOutput, MIRFile
            from .intake_storage import store_stream
            receipts, _ = register_sources(client=self.client, actor=None, channel="SFTP", workflow="835",
                sources=[(item["filename"], item["_stored_source"]) for item in items])
            source = EDI835File.objects.create(client=self.client, status="ARCHIVED")
            output = store_stream(io.BytesIO(b"SYNTHETIC MIR"), self.client.pk, max_bytes=100)
            mir = MIRFile.objects.create(source_835=source, client=self.client, mir_filename="synthetic.MIR",
                file_content="SYNTHETIC MIR", file_hash=output.checksum, file_size=output.size)
            IntakeOutput.objects.create(dispatch=receipts[0].dispatch, source_835=source, mir_file=mir,
                storage_key=output.key, checksum=output.checksum, byte_count=output.size,
                converter_version="synthetic", delivery_state="CONFIRMED")
            from .intake_service import finish_inline
            finish_inline(receipts, True, source_id=source.pk)
            return process_multiple.return_value

        process_multiple.side_effect = accept

        process_835_to_mir_sftp(self.client, self.actor)
        age_observations()
        result = process_835_to_mir_sftp(self.client, self.actor)

        self.assertTrue(result["success"])
        self.assertEqual(result["combined_mir"], "MIROUT_COMBINED.MIR")
        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(result["deleted_input_count"], 2)
        self.assertEqual(len(sftp.removed), 2)
        process_multiple.assert_called_once()
        batch = process_multiple.call_args.args[0]
        self.assertEqual([item["filename"] for item in batch], ["first.835", "second.x12"])
        self.assertTrue(process_multiple.call_args.kwargs["deliver_outbound"])

    @patch("edi835.full_sftp_pipeline.process_multiple_edi835_files")
    @patch("edi835.full_sftp_pipeline._normalize_folder", return_value="/in/835")
    @patch("edi835.full_sftp_pipeline.resolve_admin_sftp_route")
    @patch("edi835.full_sftp_pipeline._open_sftp")
    def test_inbound_sources_are_retained_when_single_combined_mir_is_not_delivered(
        self, open_sftp, resolve_route, _normalize, process_multiple
    ):
        sftp = _FakeSFTP({
            "first.835": b"ISA*FIRST~CLP*ONE~",
            "second.835": b"ISA*SECOND~CLP*TWO~",
        })
        open_sftp.return_value = (_FakeSSH(), sftp)
        resolve_route.return_value = (SimpleNamespace(), self.credentials, "/in/835")
        process_multiple.return_value = {
            "success": True,
            "accepted_files": ["first.835", "second.835"],
            "refused_files": [],
            "combined_filename": "MIROUT_COMBINED.MIR",
            "sftp_uploaded": False,
            "sftp_error": "outbound unavailable",
            "errors": [],
        }

        process_835_to_mir_sftp(self.client, self.actor)
        age_observations()
        result = process_835_to_mir_sftp(self.client, self.actor)

        self.assertFalse(result["success"])
        self.assertEqual(result["deleted_input_count"], 0)
        self.assertEqual(sftp.removed, [])
        self.assertEqual(set(result["retained_files"]), {"first.835", "second.835"})
        self.assertIn("outbound unavailable", result["errors"])
