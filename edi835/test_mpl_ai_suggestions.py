from django.test import SimpleTestCase

from edi835.mpl_ai_suggestions import _available_model_ids, _parse_claim_rewrite


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
