from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .full_sftp_pipeline import run_full_sftp_pipeline


class SFTPSourceIndependenceTestCase(SimpleTestCase):
    @patch("edi835.full_sftp_pipeline.process_835_to_mir_sftp")
    @patch("edi835.full_sftp_pipeline.process_recon_incoming")
    @patch("edi835.full_sftp_pipeline._relay_837_for_test")
    def test_same_claim_in_837_and_recon_is_processed_by_both_sources(
        self,
        relay_837,
        process_recon,
        process_835,
    ):
        """837 and RECON are independent evidence sources, even for the same claim."""
        claim_id = "86520261762674200QZL067"
        relay_837.return_value = {
            "success": True,
            "transferred_count": 1,
            "processed_count": 1,
            "claim_ids": [claim_id],
            "transferred": [{"claim_id": claim_id}],
            "errors": [],
        }
        process_recon.return_value = {
            "success": True,
            "processed_count": 1,
            "processed_files": ["same-claim.p7a"],
            "claim_ids": [claim_id],
            "errors": [],
        }
        process_835.return_value = {
            "success": True,
            "processed_count": 0,
            "errors": [],
        }

        client = SimpleNamespace(id="client-1")
        actor = SimpleNamespace(id="user-1")
        result = run_full_sftp_pipeline(client, actor)

        relay_837.assert_called_once()
        process_recon.assert_called_once_with(client, actor)
        process_835.assert_called_once_with(client, actor)
        self.assertTrue(result["success"])
        self.assertEqual(result["stages"]["837"]["claim_ids"], [claim_id])
        self.assertEqual(result["stages"]["recon"]["claim_ids"], [claim_id])

    @patch("edi835.full_sftp_pipeline.process_835_to_mir_sftp")
    @patch("edi835.full_sftp_pipeline.process_recon_incoming")
    @patch("edi835.full_sftp_pipeline._relay_837_for_test")
    def test_recon_still_runs_when_837_stage_reports_an_error(
        self,
        relay_837,
        process_recon,
        process_835,
    ):
        """A problem in one source must never suppress processing of the other source."""
        relay_837.return_value = {"success": False, "error": "837 failed", "errors": ["837 failed"]}
        process_recon.return_value = {
            "success": True,
            "processed_count": 1,
            "processed_files": ["recon.p7a"],
            "errors": [],
        }
        process_835.return_value = {"success": True, "processed_count": 0, "errors": []}

        client = SimpleNamespace(id="client-1")
        actor = SimpleNamespace(id="user-1")
        result = run_full_sftp_pipeline(client, actor)

        process_recon.assert_called_once_with(client, actor)
        process_835.assert_called_once_with(client, actor)
        self.assertFalse(result["success"])
        self.assertEqual(result["stages"]["recon"]["processed_count"], 1)
