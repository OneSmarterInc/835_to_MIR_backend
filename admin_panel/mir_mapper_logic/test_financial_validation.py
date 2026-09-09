import unittest
from decimal import Decimal

from .financial_validation import (
    UnsupportedPatientResponsibilityError,
    validate_patient_responsibility_mapping,
)
from .models import Adjustment, ServiceLine


def adjustment(group, reason, amount):
    return Adjustment(group=group, reason=reason, amount=Decimal(str(amount)))


class PatientResponsibilityFinancialSafetyTests(unittest.TestCase):
    def test_unknown_pr_reason_on_paid_line_fails_loudly(self):
        service = ServiceLine(
            charge=Decimal("200.00"),
            paid=Decimal("50.00"),
            adjustments=[adjustment("PR", "77", "150.00")],
        )

        with self.assertRaisesRegex(
            UnsupportedPatientResponsibilityError,
            r"PR77.*150\.00.*aborted",
        ):
            validate_patient_responsibility_mapping(service, "1")

    def test_known_pr_reduction_slots_remain_allowed(self):
        service = ServiceLine(
            charge=Decimal("200.00"),
            paid=Decimal("165.00"),
            adjustments=[
                adjustment("PR", "1", "10.00"),
                adjustment("PR", "2", "12.00"),
                adjustment("PR", "3", "13.00"),
            ],
        )

        validate_patient_responsibility_mapping(service, "1")

    def test_existing_pr45_special_mapping_remains_allowed(self):
        service = ServiceLine(
            charge=Decimal("100.00"),
            paid=Decimal("87.21"),
            adjustments=[adjustment("PR", "45", "12.79")],
        )

        validate_patient_responsibility_mapping(service, "1")

    def test_pr_denial_reason_remains_allowed(self):
        service = ServiceLine(
            charge=Decimal("102.00"),
            paid=Decimal("0.00"),
            adjustments=[adjustment("PR", "31", "102.00")],
        )

        validate_patient_responsibility_mapping(service, "1")

    def test_partial_pr_b11_denial_remains_allowed(self):
        service = ServiceLine(
            charge=Decimal("977.00"),
            paid=Decimal("277.24"),
            adjustments=[adjustment("PR", "B11", "699.76")],
        )

        validate_patient_responsibility_mapping(service, "1")

    def test_known_paid_reason_pr78_remains_allowed(self):
        service = ServiceLine(
            charge=Decimal("100.00"),
            paid=Decimal("50.00"),
            adjustments=[adjustment("PR", "78", "50.00")],
        )

        validate_patient_responsibility_mapping(service, "1")


if __name__ == "__main__":
    unittest.main()
