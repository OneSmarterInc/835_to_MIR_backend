import unittest
from decimal import Decimal

from .mir_mapper import (
    claim_disposition,
    mir_patient_liability,
    payment_reduction_slots,
    prepriced_amount,
    process_code_indicator,
    service_status_and_reason,
)
from .models import Adjustment, Claim, ServiceLine


def adjustment(group, reason, amount):
    return Adjustment(group=group, reason=reason, amount=Decimal(str(amount)))


class MirBusinessRuleTests(unittest.TestCase):
    def test_non_paid_clp_status_maps_to_mir_denial(self):
        claim = Claim(status="2")
        self.assertEqual(claim_disposition(claim), "4")

    def test_clp_status_2_with_co45_and_pr3_stays_paid(self):
        first_service = ServiceLine(
            charge=Decimal("89.20"),
            paid=Decimal("7.28"),
            adjustments=[
                adjustment("CO", "45", "71.92"),
                adjustment("PR", "3", "10"),
            ],
        )
        co45_service = ServiceLine(
            charge=Decimal("50"),
            paid=Decimal("5"),
            adjustments=[adjustment("CO", "45", "45")],
        )
        claim = Claim(status="2", services=[first_service, co45_service])

        self.assertEqual(claim_disposition(claim), "1")
        self.assertEqual(service_status_and_reason(first_service, claim.status), ("1", ""))
        self.assertEqual(service_status_and_reason(co45_service, claim.status), ("1", ""))

    def test_non_paid_claim_with_actual_denial_remains_denied(self):
        service = ServiceLine(
            charge=Decimal("100"),
            paid=Decimal("0"),
            adjustments=[adjustment("CO", "125", "100")],
        )
        claim = Claim(status="2", services=[service])

        self.assertEqual(claim_disposition(claim), "4")
        self.assertEqual(service_status_and_reason(service, claim.status), ("4", "CO125"))

    def test_mixed_non_paid_claim_is_not_treated_as_routine_pricing(self):
        routine = ServiceLine(
            charge=Decimal("50"),
            paid=Decimal("5"),
            adjustments=[adjustment("CO", "45", "45")],
        )
        denied = ServiceLine(
            charge=Decimal("100"),
            paid=Decimal("0"),
            adjustments=[adjustment("PR", "31", "100")],
        )
        claim = Claim(status="2", services=[routine, denied])

        self.assertEqual(claim_disposition(claim), "4")
        self.assertEqual(
            service_status_and_reason(routine, claim.status, "PR31"),
            ("4", "PR31"),
        )
        self.assertEqual(prepriced_amount(routine, claim.status, "PR31"), Decimal("5"))
        self.assertEqual(mir_patient_liability(routine, claim.status, "PR31"), Decimal("0"))
        self.assertEqual(service_status_and_reason(denied, claim.status), ("4", "PR31"))

    def test_partial_pr_b11_is_claim_and_line_denial(self):
        service = ServiceLine(
            charge=Decimal("977"),
            paid=Decimal("277.24"),
            adjustments=[adjustment("PR", "B11", "699.76")],
        )
        claim = Claim(status="1", total_paid=Decimal("277.24"), services=[service])
        self.assertEqual(claim_disposition(claim), "4")
        self.assertEqual(service_status_and_reason(service, claim.status), ("4", "PRB11"))
        self.assertEqual(prepriced_amount(service, claim.status), Decimal("977"))
        self.assertEqual(mir_patient_liability(service, claim.status), Decimal("699.76"))

    def test_pi_b11_with_ordinary_pr_stays_paid(self):
        service = ServiceLine(
            charge=Decimal("6594.50"),
            paid=Decimal("0"),
            adjustments=[
                adjustment("PI", "B11", "6594.50"),
                adjustment("PR", "3", "100"),
            ],
        )
        claim = Claim(status="1", total_paid=Decimal("0"), services=[service])
        self.assertEqual(claim_disposition(claim), "1")
        self.assertEqual(service_status_and_reason(service, claim.status), ("1", ""))

    def test_pr31_denies_line_but_not_paid_claim(self):
        service = ServiceLine(
            charge=Decimal("102"),
            paid=Decimal("0"),
            adjustments=[
                adjustment("CO", "45", "61.02"),
                adjustment("PR", "31", "40.98"),
            ],
        )
        claim = Claim(status="1", total_paid=Decimal("0"), services=[service])
        self.assertEqual(claim_disposition(claim), "1")
        self.assertEqual(service_status_and_reason(service, claim.status), ("4", "PR31"))
        self.assertEqual(prepriced_amount(service, claim.status), Decimal("40.98"))
        self.assertEqual(mir_patient_liability(service, claim.status), Decimal("40.98"))

    def test_process_indicator_uses_aa_or_frequency_6(self):
        self.assertEqual(process_code_indicator(Claim(facility_type="AA")), "A")
        self.assertEqual(process_code_indicator(Claim(claim_frequency="6")), "A")
        self.assertEqual(process_code_indicator(Claim(claim_frequency="8")), "")

    def test_pr45_maps_to_reference_pr119_slot(self):
        service = ServiceLine(adjustments=[adjustment("PR", "45", "12.79")])
        self.assertEqual(payment_reduction_slots(service), {3: ("PR119", Decimal("12.79"))})

    def test_slot_3_rejects_mixed_pr3_and_pr45(self):
        for adjustments in (
            [adjustment("PR", "3", "10"), adjustment("PR", "45", "12.79")],
            [adjustment("PR", "45", "12.79"), adjustment("PR", "3", "10")],
        ):
            with self.subTest(adjustments=adjustments):
                service = ServiceLine(adjustments=adjustments)
                with self.assertRaisesRegex(
                    ValueError,
                    r"MIR reduction slot 3 cannot contain both (PR3 and PR119|PR119 and PR3)",
                ):
                    payment_reduction_slots(service)


if __name__ == "__main__":
    unittest.main()
