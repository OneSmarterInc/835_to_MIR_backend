from django.test import SimpleTestCase

from .claim_numbers import split_claim_number


class ClaimNumberSplitTests(SimpleTestCase):
    def test_numeric_prefix_and_alphabetic_remainder_are_split(self):
        self.assertEqual(split_claim_number("86520261762674200QZL067"), {
            "highmark_claim_number": "86520261762674200",
            "internal_claim_number": "QZL067",
        })

    def test_alphabetic_identifier_remains_internal(self):
        self.assertEqual(split_claim_number("CLAIM-1"), {
            "highmark_claim_number": "",
            "internal_claim_number": "CLAIM-1",
        })
