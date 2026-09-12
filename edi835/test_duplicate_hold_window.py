from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from accounts.models import Client
from edi835.held_claims import (
    DUPLICATE_HOLD_WINDOW,
    _candidate_rows,
    duplicate_eligible_send_at,
    recent_sent_claim_history,
)
from edi835.models import EDI835File, MIRClaim, MIRFile


class DuplicateHoldWindowTests(TestCase):
    def setUp(self):
        self.client = Client.objects.create(
            name="Duplicate Test Client",
            client_code="DUP-TEST",
            email="duplicate@example.com",
        )
        self.other_client = Client.objects.create(
            name="Other Client",
            client_code="DUP-OTHER",
            email="other@example.com",
        )

    def _sent_claim(self, claim_number, sent_at, *, client=None, status="PUSHED"):
        client = client or self.client
        source = EDI835File.objects.create(
            client=client,
            original_filename=f"{claim_number}.835",
            stored_filename=f"{claim_number}.835",
            status="ARCHIVED",
        )
        mir_file = MIRFile.objects.create(
            source_835=source,
            client=client,
            mir_filename=f"{claim_number}.MIR",
            file_content="test",
            file_hash=(claim_number.lower() + "0" * 64)[:64],
            file_size=4,
            claim_count=1,
            physical_row_count=1,
            service_count=0,
            status=status,
        )
        MIRFile.objects.filter(id=mir_file.id).update(updated_at=sent_at)
        mir_file.refresh_from_db()
        MIRClaim.objects.create(
            mir_file=mir_file,
            claim_sequence=1,
            claim_control_number=f"{claim_number}REF001",
            header_raw=" " * 334,
        )
        return mir_file

    def test_fourth_day_is_eligible_at_530_pm_eastern(self):
        sent_at = datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc)
        self._sent_claim("CLAIM100", sent_at)

        before_deadline = datetime(2026, 9, 4, 21, 29, tzinfo=dt_timezone.utc)
        history = recent_sent_claim_history(
            self.client,
            {"CLAIM100"},
            now=before_deadline,
        )
        self.assertIn("CLAIM100", history)
        self.assertEqual(
            history["CLAIM100"]["eligible_send_at"],
            datetime(2026, 9, 4, 21, 30, tzinfo=dt_timezone.utc),
        )

        at_deadline = datetime(2026, 9, 4, 21, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(
            recent_sent_claim_history(self.client, {"CLAIM100"}, now=at_deadline),
            {},
        )

    def test_530_pm_eastern_is_dst_aware(self):
        winter_sent_at = datetime(2026, 1, 1, 15, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(
            duplicate_eligible_send_at(winter_sent_at),
            datetime(2026, 1, 4, 22, 30, tzinfo=dt_timezone.utc),
        )

    def test_fourth_calendar_day_uses_three_day_date_offset(self):
        self.assertEqual(DUPLICATE_HOLD_WINDOW.total_seconds(), 72 * 60 * 60)

    def test_legacy_stored_deadline_uses_new_530_pm_eastern_release_time(self):
        source = EDI835File.objects.create(
            client=self.client,
            original_filename="legacy.835",
            stored_filename="legacy.835",
            status="ARCHIVED",
            input_file_content="legacy source content",
            held_claims_count=1,
            conversion_findings=[{
                "rule_code": "DUPLICATE_RECENT_MIR",
                "severity": "HOLD",
                "release_status": "HELD",
                "claim_index": "1",
                "claim_number": "LEGACY100",
                "previous_sent_at": "2026-09-01T10:00:00+00:00",
                "eligible_send_at": "2026-09-04T10:00:00+00:00",
            }],
        )

        before_new_deadline = datetime(2026, 9, 4, 21, 29, tzinfo=dt_timezone.utc)
        self.assertEqual(_candidate_rows(before_new_deadline, 25), [])

        at_new_deadline = datetime(2026, 9, 4, 21, 30, tzinfo=dt_timezone.utc)
        candidates = _candidate_rows(at_new_deadline, 25)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_id"], str(source.id))
        self.assertEqual(
            candidates[0]["eligible_send_at"],
            datetime(2026, 9, 4, 21, 30, tzinfo=dt_timezone.utc),
        )

    def test_only_pushed_history_for_same_client_counts(self):
        sent_at = datetime(2026, 9, 2, 9, 30, tzinfo=dt_timezone.utc)
        now = datetime(2026, 9, 3, 9, 30, tzinfo=dt_timezone.utc)
        self._sent_claim("NOTPUSHED", sent_at, status="GENERATED")
        self._sent_claim("OTHERCLIENT", sent_at, client=self.other_client)

        history = recent_sent_claim_history(
            self.client,
            {"NOTPUSHED", "OTHERCLIENT"},
            now=now,
        )
        self.assertEqual(history, {})
