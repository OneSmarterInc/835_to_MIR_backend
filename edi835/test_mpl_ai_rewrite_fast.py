from unittest.mock import patch

from django.test import SimpleTestCase

from edi835.mpl_ai_rewrite_fast import _rewrite_one_claim_single_call


class MPLAIBatchedRewriteTests(SimpleTestCase):
    @patch("edi835.mpl_ai_rewrite_fast._qwen_text")
    def test_one_model_call_returns_natural_paragraph_and_actions(self, qwen_text):
        qwen_text.return_value = (
            '{"paragraph":"Review the available claim evidence, verify reconciliation coverage, and correct only the approved source or mapping before regenerating the MIR.",'
            '"bullets":["Confirm the claim was included in the intended conversion batch.",'
            '"Verify reconciliation covers the claim and reporting period.",'
            '"Correct the approved source or mapping, then regenerate and validate the MIR before transmission."]}'
        )
        result = _rewrite_one_claim_single_call(
            "http://127.0.0.1:8080/v1",
            "qwen3-0.6b-instruct-q8_0",
            {"Content-Type": "application/json"},
            "86520262000982500",
            [
                "Confirm the claim was included in the intended conversion batch.",
                "Confirm that reconciliation covers this claim and reporting period.",
                "Correct the approved source or mapping and regenerate the affected output.",
                "Validate the MIR before transmission.",
            ],
        )
        self.assertEqual(qwen_text.call_count, 1)
        self.assertEqual(len(result["bullets"]), 3)
        self.assertTrue(result["paragraph"].startswith("Review the available claim evidence"))

    @patch("edi835.mpl_ai_rewrite_fast._qwen_text")
    def test_accepts_json_wrapped_in_model_chatter(self, qwen_text):
        qwen_text.return_value = (
            'Here is the recommendation:\n'
            '{"paragraph":"Review the approved evidence before making changes.",'
            '"bullets":["Verify the reconciliation record before reprocessing."]}\n'
            'Done.'
        )
        result = _rewrite_one_claim_single_call(
            "http://127.0.0.1:8080/v1",
            "qwen3-0.6b-instruct-q8_0",
            {"Content-Type": "application/json"},
            "86520262000982500",
            ["Verify the reconciliation record before reprocessing."],
        )
        self.assertEqual(qwen_text.call_count, 1)
        self.assertEqual(result["bullets"], ["Verify the reconciliation record before reprocessing."])

    @patch("edi835.mpl_ai_rewrite_fast._qwen_text")
    def test_rejects_unusable_output_without_per_bullet_fallback(self, qwen_text):
        qwen_text.return_value = "I recommend reviewing the claim."
        with self.assertRaises(ValueError):
            _rewrite_one_claim_single_call(
                "http://127.0.0.1:8080/v1",
                "qwen3-0.6b-instruct-q8_0",
                {"Content-Type": "application/json"},
                "86520262000982500",
                ["Review the claim."],
            )
        self.assertEqual(qwen_text.call_count, 1)
