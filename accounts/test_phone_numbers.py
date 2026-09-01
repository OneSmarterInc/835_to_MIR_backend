from django.test import SimpleTestCase

from accounts.phone_numbers import normalize_phone_number


class PhoneNumberValidationTestCase(SimpleTestCase):
    def test_normalizes_valid_country_specific_mobile_numbers(self):
        self.assertEqual(normalize_phone_number("9876543210", "IN"), "+919876543210")
        self.assertEqual(normalize_phone_number("4155552671", "US"), "+14155552671")
        self.assertEqual(normalize_phone_number("07400123456", "GB"), "+447400123456")

    def test_rejects_invalid_length_prefix_and_landline(self):
        for raw, region in (("98765", "IN"), ("1234567890", "IN"), ("12345", "US")):
            with self.subTest(raw=raw, region=region):
                with self.assertRaises(ValueError):
                    normalize_phone_number(raw, region)

    def test_rejects_number_that_does_not_match_selected_country(self):
        with self.assertRaisesRegex(ValueError, "selected country code"):
            normalize_phone_number("+919876543210", "US")
