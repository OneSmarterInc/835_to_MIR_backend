import io
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .full_sftp_pipeline import process_835_to_mir_sftp


class _FakeSSH:
    def close(self):
        pass


class _FakeSFTP:
    def __init__(self, files):
        self.files = dict(files)
        self.removed = []

    def listdir_attr(self, folder):
        return [SimpleNamespace(filename=name, st_mode=0) for name in self.files]

    def open(self, path, mode):
        name = path.rsplit("/", 1)[-1]
        return io.BytesIO(self.files[name])

    def remove(self, path):
        name = path.rsplit("/", 1)[-1]
        self.removed.append(path)
        self.files.pop(name, None)

    def close(self):
        pass


class CombinedSFTP835ContractTests(SimpleTestCase):
    def setUp(self):
        self.client = SimpleNamespace(id="client-1")
        self.actor = SimpleNamespace(is_authenticated=True)
        self.credentials = {"remote_folder": "/in/835"}

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

        result = process_835_to_mir_sftp(self.client, self.actor)

        self.assertFalse(result["success"])
        self.assertEqual(result["deleted_input_count"], 0)
        self.assertEqual(sftp.removed, [])
        self.assertEqual(set(result["retained_files"]), {"first.835", "second.835"})
        self.assertIn("outbound unavailable", result["errors"])
