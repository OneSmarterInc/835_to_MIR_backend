from django.test import TestCase

from accounts.models import Client
from edi835.long_hold_alerts import (
    RESOLUTION_STATUS_FIELD,
    RESOLVED_AT_FIELD,
    RESOLVED_MIR_FIELD,
    RESOLVED_SOURCE_FIELD,
)
from edi835.mir_persistence import set_mir_push_status
from edi835.models import EDI835File, MIRClaim, MIRFile


class CorrectedClaimResolutionTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Corrected Claim Client",
            client_code="CORRECTED-CLAIM",
            email="operations@example.com",
        )

    def _held_source(self, *, rule_code="MP003", claim_number="CLAIM100"):
        return EDI835File.objects.create(
            client=self.client_record,
            original_filename="original-held.835",
            stored_filename="original-held.835",
            status="ARCHIVED",
            held_claims_count=1,
            conversion_findings=[{
                "rule_code": rule_code,
                "severity": "HOLD",
                "claim_index": "1",
                "claim_number": claim_number,
                "reason": "Claim requires correction before delivery.",
            }],
        )

    def _corrected_mir(self, claim_number="CLAIM100"):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="corrected.835",
            stored_filename="corrected.835",
            status="ARCHIVED",
            ingestion_source="SFTP",
        )
        mir_file = MIRFile.objects.create(
            source_835=source,
            client=self.client_record,
            mir_filename="CORRECTED.MIR",
            file_content="test",
            file_hash="a" * 64,
            file_size=4,
            claim_count=1,
            physical_row_count=1,
            service_count=0,
            status="GENERATED",
        )
        MIRClaim.objects.create(
            mir_file=mir_file,
            claim_sequence=1,
            claim_control_number=claim_number,
            header_raw=" " * 334,
        )
        return mir_file

    def test_nonduplicate_hold_resolves_only_after_corrected_mir_is_pushed(self):
        held = self._held_source()
        mir_file = self._corrected_mir()

        held.refresh_from_db()
        self.assertNotEqual(
            held.conversion_findings[0].get(RESOLUTION_STATUS_FIELD),
            "RESOLVED",
        )
        self.assertEqual(held.held_claims_count, 1)

        set_mir_push_status(mir_file, True)

        held.refresh_from_db()
        finding = held.conversion_findings[0]
        self.assertEqual(finding.get(RESOLUTION_STATUS_FIELD), "RESOLVED")
        self.assertTrue(finding.get(RESOLVED_AT_FIELD))
        self.assertEqual(finding.get(RESOLVED_MIR_FIELD), "CORRECTED.MIR")
        self.assertEqual(finding.get(RESOLVED_SOURCE_FIELD), "corrected.835")
        self.assertEqual(held.held_claims_count, 0)

    def test_duplicate_hold_is_not_marked_resolved_by_corrected_claim_hook(self):
        held = self._held_source(rule_code="DUPLICATE_RECENT_MIR")
        mir_file = self._corrected_mir()

        set_mir_push_status(mir_file, True)

        held.refresh_from_db()
        finding = held.conversion_findings[0]
        self.assertNotEqual(finding.get(RESOLUTION_STATUS_FIELD), "RESOLVED")
        self.assertEqual(held.held_claims_count, 1)
