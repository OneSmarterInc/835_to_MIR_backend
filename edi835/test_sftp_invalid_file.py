import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from accounts.models import Client, User
from .models import EDI835File
from .sftp_automation_operations import ingest_835_incoming


class SFTPInvalid835RegressionTestCase(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="N11 Client", client_code="N1101", email="n11@example.com"
        )
        self.admin = User.objects.create_superuser(
            email="n11-admin@example.com",
            name="N11 Admin",
            mobile="1000000000",
            password="test-password",
        )

    def _sftp(self, filename="bad.835", content=b"not-a-valid-835"):
        ssh = MagicMock()
        sftp = MagicMock()
        sftp.listdir_attr.return_value = [
            SimpleNamespace(filename=filename, st_mode=stat.S_IFREG)
        ]
        handle = MagicMock()
        handle.read.return_value = content
        sftp.open.return_value.__enter__.return_value = handle
        return ssh, sftp

    @patch("edi835.sftp_automation_operations.relative_media_path", return_value="clients/n11/835/archive/bad.835")
    @patch("edi835.sftp_automation_operations.archive_inbound", return_value=Path("/tmp/archive/bad.835"))
    @patch("edi835.sftp_automation_operations.stage_inbound", return_value=Path("/tmp/inbound/bad.835"))
    @patch("edi835.sftp_automation_operations.validate_835_content")
    @patch("edi835.sftp_automation_operations._normalize_folder", return_value="/in/835")
    @patch("edi835.sftp_automation_operations._connected")
    @patch("edi835.sftp_automation_operations._open_sftp")
    def test_structured_validation_errors_are_persisted_as_json_and_remote_is_removed(
        self, open_sftp, connected, _normalize, validate, _stage, _archive, _relative
    ):
        ssh, sftp = self._sftp()
        open_sftp.return_value = (ssh, sftp)
        connected.return_value = (
            None,
            {"remote_folder": "/in/835"},
        )
        validate.return_value = (
            False,
            {
                "valid": False,
                "decision": "REFUSE",
                "errors": [{"message": "Missing ISA segment", "segment": "ISA"}],
                "warnings": [],
                "findings": [
                    {
                        "message": "Missing ISA segment",
                        "segment": "ISA",
                        "severity": "REFUSE",
                        "decision": "REFUSE",
                    }
                ],
            },
        )

        result = ingest_835_incoming(self.client_record, self.admin)

        record = EDI835File.objects.get(original_filename="bad.835")
        payload = json.loads(record.error_message)
        self.assertEqual(record.status, "ERROR")
        self.assertEqual(record.ingestion_source, "SFTP")
        self.assertEqual(payload["type"], "835_validation_error")
        self.assertEqual(payload["decision"], "REFUSE")
        self.assertEqual(payload["errors"][0]["segment"], "ISA")
        self.assertEqual(result["errors"], ["bad.835: 835 validation failed"])
        sftp.remove.assert_called_once_with("/in/835/bad.835")

    @patch("edi835.sftp_automation_operations.relative_media_path", return_value="clients/n11/835/archive/bad.835")
    @patch("edi835.sftp_automation_operations.archive_inbound", return_value=Path("/tmp/archive/bad.835"))
    @patch("edi835.sftp_automation_operations.stage_inbound", return_value=Path("/tmp/inbound/bad.835"))
    @patch("edi835.sftp_automation_operations.validate_835_content")
    @patch("edi835.sftp_automation_operations._normalize_folder", return_value="/in/835")
    @patch("edi835.sftp_automation_operations._connected")
    @patch("edi835.sftp_automation_operations._open_sftp")
    @patch("edi835.sftp_automation_operations.EDI835File.objects.create")
    def test_remote_file_is_retained_when_error_record_persistence_fails(
        self, create_record, open_sftp, connected, _normalize, validate, _stage, _archive, _relative
    ):
        ssh, sftp = self._sftp()
        open_sftp.return_value = (ssh, sftp)
        connected.return_value = (None, {"remote_folder": "/in/835"})
        validate.return_value = (
            False,
            {"decision": "REFUSE", "errors": [{"message": "Invalid 835"}]},
        )
        create_record.side_effect = RuntimeError("database unavailable")

        result = ingest_835_incoming(self.client_record, self.admin)

        sftp.remove.assert_not_called()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("database unavailable", result["errors"][0])

    @patch("edi835.sftp_automation_operations.archive_inbound")
    @patch("edi835.sftp_automation_operations.stage_inbound", return_value=Path("/tmp/inbound/bad.835"))
    @patch("edi835.sftp_automation_operations.validate_835_content")
    @patch("edi835.sftp_automation_operations._normalize_folder", return_value="/in/835")
    @patch("edi835.sftp_automation_operations._connected")
    @patch("edi835.sftp_automation_operations._open_sftp")
    def test_remote_file_is_retained_when_archive_fails(
        self, open_sftp, connected, _normalize, validate, _stage, archive
    ):
        ssh, sftp = self._sftp()
        open_sftp.return_value = (ssh, sftp)
        connected.return_value = (None, {"remote_folder": "/in/835"})
        validate.return_value = (
            False,
            {"decision": "REFUSE", "errors": [{"message": "Invalid 835"}]},
        )
        archive.side_effect = OSError("archive unavailable")

        result = ingest_835_incoming(self.client_record, self.admin)

        sftp.remove.assert_not_called()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("archive unavailable", result["errors"][0])
