import unittest
from datetime import date
from decimal import Decimal

from . import config
from .mir_generator import generate_mir_records, generate_mir_text
from .mir_mapper import co_adjustment_total, covered_charge, patient_liability
from .models import Adjustment, Claim, ServiceLine


def service(charge="100", paid="0", adjustments=None):
    return ServiceLine(
        procedure="HC",
        charge=Decimal(charge),
        paid=Decimal(paid),
        adjustments=adjustments or [],
    )


class HeldClaimGenerationTests(unittest.TestCase):
    def test_stored_process_date_makes_regeneration_byte_identical(self):
        claim = Claim(claim_number="REPEAT", status="1", group_number="GROUP1", services=[service("100", "80")])
        stored_date = date(2026, 8, 31)

        first, _ = generate_mir_text([claim], process_date=stored_date)
        second, _ = generate_mir_text([claim], process_date=stored_date)

        self.assertEqual(first, second)
        self.assertEqual(first.splitlines()[0][36:52], "2026083120260831")

    def test_financial_values_are_not_silently_clamped(self):
        line = service("100", "25", [Adjustment("CO", "45", Decimal("125"))])
        self.assertEqual(co_adjustment_total(line), Decimal("125"))
        self.assertEqual(covered_charge(line), Decimal("-25"))
        self.assertEqual(patient_liability(line), Decimal("-50"))

    def test_bad_claim_is_held_while_valid_claim_is_delivered(self):
        bad = Claim(
            claim_number="BAD",
            group_number="GROUP1",
            services=[service("100", "25", [Adjustment("CO", "45", Decimal("125"))])],
        )
        good = Claim(claim_number="GOOD", status="1", group_number="GROUP1", services=[service("100", "80")])

        records, summary = generate_mir_records([bad, good])

        self.assertEqual(len(records), 1)
        self.assertEqual(summary["claims"], 2)
        self.assertEqual(summary["delivered_claims"], 1)
        self.assertEqual(summary["held_claims"], 1)
        self.assertEqual(
            {finding["rule_code"] for finding in summary["findings"]},
            {"CO_EXCEEDS_CHARGE", "NEGATIVE_COVERED_CHARGE", "MP003"},
        )

    def test_unknown_patient_responsibility_reason_is_retained_as_finding(self):
        claim = Claim(
            claim_number="PR31",
            services=[service("100", "0", [Adjustment("PR", "31", Decimal("100"))])],
        )

        records, summary = generate_mir_records([claim])

        self.assertEqual(records, [])
        self.assertEqual(summary["held_claims"], 1)
        self.assertEqual(summary["findings"][0]["rule_code"], "UNMAPPED_PR_REASON")
        self.assertEqual(summary["findings"][0]["adjustment_amount"], "100")

    def test_pr_b11_denial_is_delivered_without_an_unmapped_reason_finding(self):
        claim = Claim(
            claim_number="B11-DENIAL",
            status="2",
            group_number="10670170",
            services=[service("31", "0", [Adjustment("PR", "B11", Decimal("31"))])],
        )

        records, summary = generate_mir_records([claim])

        self.assertEqual(len(records), 1)
        self.assertEqual(summary["delivered_claims"], 1)
        self.assertEqual(summary["held_claims"], 0)
        self.assertEqual(summary["findings"], [])

    def test_field_overflow_holds_only_the_affected_claim(self):
        bad = Claim(claim_number="BAD", group_number="GROUP1", services=[service("100000000")])
        good = Claim(claim_number="GOOD", status="1", group_number="GROUP1", services=[service()])

        records, summary = generate_mir_records([bad, good])

        self.assertEqual(len(records), 1)
        self.assertEqual(summary["held_claims"], 1)
        self.assertEqual(summary["findings"][0]["rule_code"], "MIR_FIELD_OVERFLOW")

    def test_record_sequence_overflow_holds_only_oversized_claim(self):
        count = config.MAX_SERVICE_LINES_PER_RECORD * config.MAX_RECORD_SEQUENCE + 1
        oversized = Claim(claim_number="LARGE", group_number="GROUP1", services=[service()] * count)
        valid = Claim(claim_number="GOOD", status="1", group_number="GROUP1", services=[service()])

        records, summary = generate_mir_records([oversized, valid])

        self.assertEqual(len(records), 1)
        self.assertEqual(summary["delivered_claims"], 1)
        self.assertEqual(summary["held_claims"], 1)
        finding = summary["findings"][0]
        self.assertEqual(finding["rule_code"], "RECORD_SEQUENCE_LIMIT_EXCEEDED")
        self.assertEqual(finding["claim_number"], "LARGE")
        self.assertEqual(finding["service_count"], str(count))
        self.assertEqual(
            finding["maximum_services"],
            str(config.MAX_SERVICE_LINES_PER_RECORD * config.MAX_RECORD_SEQUENCE),
        )
        self.assertEqual(finding["required_records"], "100")
        self.assertEqual(finding["maximum_records"], "99")


if __name__ == "__main__":
    unittest.main()
