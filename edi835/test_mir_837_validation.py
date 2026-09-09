from decimal import Decimal

from django.test import TestCase

from accounts.models import Client
from admin_panel.mir_mapper_logic import config
from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

from .mir_837_validation import (
    MIR837ConsistencyError,
    validate_generated_mir_against_837,
)
from .models import EDI837Claim, EDI837File, EDI837ServiceLine


def _amount(value):
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "+" if amount >= 0 else "-"
    digits = f"{abs(amount):.2f}".replace(".", "").rjust(10, "0")
    return f"{digits}{sign}"


def _mir_text(charges):
    header = [" "] * config.MIR_HEADER_LENGTH
    header[2:19] = list("12345678901234567")
    header[248:250] = list("01")
    header[250:252] = list("01")
    header[332:334] = list(str(len(charges)).rjust(2, "0"))
    blocks = []
    for charge in charges:
        block = [" "] * config.MIR_SERVICE_BLOCK_LENGTH
        block[50:61] = list(_amount(charge))
        blocks.append("".join(block))
    return "".join(header) + "".join(blocks) + "\r\n"


class MIR837ConsistencyTests(TestCase):
    def setUp(self):
        self.client = Client.objects.create(
            name="B2 Test Client",
            client_code="B2TEST",
            email="b2@example.com",
        )

    def source_claim(self, charges=("50.00", "75.00")):
        services = [ServiceLine(charge=Decimal(value)) for value in charges]
        return Claim(
            claim_number="12345678901234567",
            total_charge=sum((svc.charge for svc in services), Decimal("0.00")),
            services=services,
        )

    def store_837(self, charges=("50.00", "75.00"), claim_number="12345678901234567"):
        edi_file = EDI837File.objects.create(
            client=self.client,
            original_filename="source.837",
            stored_filename="source.837",
            file_content="837",
            file_hash="a" * 64,
            status="PROCESSED",
            claim_count=1,
            service_count=len(charges),
            total_charge_amount=sum((Decimal(value) for value in charges), Decimal("0.00")),
        )
        claim = EDI837Claim.objects.create(
            edi_file=edi_file,
            client=self.client,
            claim_sequence=1,
            claim_control_number=claim_number,
            highmark_claim_number=claim_number,
            patient_control_number=claim_number,
            service_count=len(charges),
            total_charge_amount=sum((Decimal(value) for value in charges), Decimal("0.00")),
        )
        for sequence, value in enumerate(charges, start=1):
            EDI837ServiceLine.objects.create(
                claim=claim,
                edi_file=edi_file,
                service_sequence=sequence,
                charge_amount=Decimal(value),
            )
        return claim

    def test_no_stored_837_does_not_break_existing_conversion(self):
        report = validate_generated_mir_against_837(
            client=self.client,
            claims=[self.source_claim()],
            mir_text=_mir_text(("50.00", "75.00")),
        )
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["blocking"], [])

    def test_matching_837_and_mir_pass(self):
        self.store_837()
        report = validate_generated_mir_against_837(
            client=self.client,
            claims=[self.source_claim()],
            mir_text=_mir_text(("50.00", "75.00")),
        )
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["blocking"], [])

    def test_missing_mir_service_line_fails(self):
        self.store_837()
        with self.assertRaisesRegex(MIR837ConsistencyError, "generated MIR has 1 / 50.00"):
            validate_generated_mir_against_837(
                client=self.client,
                claims=[self.source_claim()],
                mir_text=_mir_text(("50.00",)),
            )

    def test_mir_charge_mismatch_fails(self):
        self.store_837()
        with self.assertRaisesRegex(MIR837ConsistencyError, "generated MIR has 2 / 124.00"):
            validate_generated_mir_against_837(
                client=self.client,
                claims=[self.source_claim()],
                mir_text=_mir_text(("50.00", "74.00")),
            )

    def test_837_835_source_disagreement_is_non_blocking(self):
        self.store_837(("50.00", "75.00"))
        report = validate_generated_mir_against_837(
            client=self.client,
            claims=[self.source_claim(("50.00", "70.00"))],
            mir_text=_mir_text(("50.00", "70.00")),
        )
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertTrue(report["warnings"])

    def test_duplicate_identical_837_intakes_are_not_ambiguous(self):
        self.store_837()
        self.store_837()
        report = validate_generated_mir_against_837(
            client=self.client,
            claims=[self.source_claim()],
            mir_text=_mir_text(("50.00", "75.00")),
        )
        self.assertEqual(report["checked"], 1)

    def test_conflicting_837_candidates_are_not_guessed(self):
        self.store_837(("50.00", "75.00"))
        self.store_837(("40.00", "85.00"))
        report = validate_generated_mir_against_837(
            client=self.client,
            claims=[self.source_claim()],
            mir_text=_mir_text(("50.00", "75.00")),
        )
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertIn("conflicting", report["warnings"][0])
