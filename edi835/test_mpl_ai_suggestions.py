from unittest.mock import patch

from django.test import SimpleTestCase

from edi835.mpl_ai_suggestions import (
    _available_model_ids,
    _parse_claim_rewrite,
    _qwen_api_key,
    _rewrite_one_claim,
)


class MPLAISuggestionParsingTests(SimpleTestCase):
    def test_accepts_exact_json_shape(self):
        result = _parse_claim_rewrite(
            '{"paragraph":"Review the claim using the approved steps.","bullets":["Compare the charges.","Regenerate the output."]}',
            2,
        )
        self.assertEqual(result["paragraph"], "Review the claim using the approved steps.")
        self.assertEqual(result["bullets"], ["Compare the charges.", "Regenerate the output."])

    def test_accepts_plain_text_fallback_from_small_model(self):
        result = _parse_claim_rewrite(
            "Review the claim using the approved steps.\n1. Compare the charges.\n2. Regenerate the output.",
            2,
        )
        self.assertEqual(result["paragraph"], "Review the claim using the approved steps.")
        self.assertEqual(result["bullets"], ["Compare the charges.", "Regenerate the output."])

    def test_rejects_changed_bullet_count(self):
        result = _parse_claim_rewrite(
            '{"paragraph":"Review the claim.","bullets":["Only one item."]}',
            2,
        )
        self.assertIsNone(result)

    def test_reads_llama_cpp_models_shape(self):
        self.assertEqual(
            _available_model_ids({
                "models": [{
                    "name": "qwen3-0.6b-instruct-q8_0",
                    "model": "qwen3-0.6b-instruct-q8_0",
                }]
            }),
            ["qwen3-0.6b-instruct-q8_0"],
        )

    def test_reads_openai_models_shape(self):
        self.assertEqual(
            _available_model_ids({
                "data": [{"id": "qwen3-0.6b-instruct-q8_0", "object": "model"}]
            }),
            ["qwen3-0.6b-instruct-q8_0"],
        )

    @patch("edi835.mpl_ai_suggestions._qwen_text")
    def test_rewrites_paragraph_and_each_bullet_independently(self, qwen_text):
        qwen_text.side_effect = [
            "Review the claim using the approved corrective steps.",
            "Compare the source charges with reconciliation.",
            "Regenerate the corrected output after validation.",
        ]
        result = _rewrite_one_claim(
            "http://127.0.0.1:8080/v1",
            "qwen3-0.6b-instruct-q8_0",
            {"Content-Type": "application/json"},
            "86520262000982500",
            [
                "Compare source 837 charges with the reconciliation values.",
                "Correct the approved source or mapping and regenerate the affected output.",
            ],
        )
        self.assertEqual(
            result,
            {
                "paragraph": "Review the claim using the approved corrective steps.",
                "bullets": [
                    "Compare the source charges with reconciliation.",
                    "Regenerate the corrected output after validation.",
                ],
            },
        )
        self.assertEqual(qwen_text.call_count, 3)

    @patch("edi835.mpl_ai_suggestions._read_env_value", return_value="server-secret")
    @patch.dict("os.environ", {"MPL_AI_API_KEY": "worker-secret"}, clear=False)
    def test_local_qwen_prefers_server_api_key(self, _read_env_value):
        self.assertEqual(
            _qwen_api_key("http://127.0.0.1:8080/v1"),
            "server-secret",
        )
