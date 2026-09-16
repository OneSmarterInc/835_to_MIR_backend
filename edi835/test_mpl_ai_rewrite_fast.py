from unittest.mock import patch

from django.test import SimpleTestCase

from edi835.mpl_ai_rewrite_fast import _rewrite_one_claim_single_call


class MPLAIBatchedRewriteTests(SimpleTestCase):
    @patch("edi835.mpl_ai_rewrite_fast._qwen_text")
    def test_one_model_call_returns_paragraph_and_matching_bullets(self, qwen_text):
        qwen_text.return_value = (
            '{"paragraph":"Review the claim using the approved corrective steps.",'
            '"bullets":["Compare the source charges with reconciliation.",'
            '"Regenerate the corrected output after validation."]}'
        )
        result = _rewrite_one_claim_single_call(
            "http://127.0.0.1:8080/v1",
            "qwen3-0.6b-instruct-q8_0",
            {"Content-Type": "application/json"},
            "86520262000982500",
            [
                "Compare source 837 charges with the reconciliation values.",
                "Correct the approved source or mapping and regenerate the affected output.",
            ],
        )
        self.assertEqual(qwen_text.call_count, 1)
        self.assertEqual(len(result["bullets"]), 2)
        self.assertEqual(
            result["paragraph"],
            "Review the claim using the approved corrective steps.",
        )

    @patch("edi835.mpl_ai_rewrite_fast._qwen_text")
    def test_rejects_wrong_bullet_count(self, qwen_text):
        qwen_text.return_value = '{"paragraph":"Review it.","bullets":["Only one."]}'
        with self.assertRaises(ValueError):
            _rewrite_one_claim_single_call(
                "http://127.0.0.1:8080/v1",
                "qwen3-0.6b-instruct-q8_0",
                {"Content-Type": "application/json"},
                "86520262000982500",
                ["First approved action.", "Second approved action."],
            )
