import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType


def normalize_phone_number(value, country_code=None, *, required=True):
    """Validate and return a canonical E.164 phone number."""
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("Mobile number is required.")
        return ""

    region = str(country_code or "").strip().upper() or None
    if region and (len(region) != 2 or not region.isalpha()):
        raise ValueError("Select a valid country code.")

    # A separately selected country must control parsing. National inputs may
    # contain harmless formatting, but not another international prefix.
    if region and raw.startswith("+"):
        parsed_region = None
    else:
        parsed_region = region
    try:
        parsed = phonenumbers.parse(raw, parsed_region)
    except NumberParseException as exc:
        raise ValueError("Enter a valid mobile number for the selected country code.") from exc

    if region and phonenumbers.region_code_for_number(parsed) not in {region, None}:
        raise ValueError("The mobile number does not match the selected country code.")
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        digits = re.sub(r"\D", "", raw)
        raise ValueError(
            f"Enter a valid mobile number for {region or 'the selected country'} "
            f"({len(digits)} national digits received)."
        )
    if phonenumbers.number_type(parsed) not in {
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }:
        raise ValueError("Enter a valid mobile number, not a landline or service number.")
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
