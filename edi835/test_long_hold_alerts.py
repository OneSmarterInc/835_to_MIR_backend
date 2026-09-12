from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase

from accounts.models import Client
from edi835.long_hold_alerts import (
    ALERT_COUNT_FIELD,
    ALERT_FIELD,
    ALERT_LAST_FIELD,
    RESOLUTION_STATUS_FIELD,
    RESOLVED_MIR_FIELD,
    send_overdue_nonduplicate_hold_alerts,
)
from edi835.models import EDI835File, MIRClaim, MIRFile


class LongHoldAlertTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Long Hold Client",
            client_code="LONG-HOLD",
            email="operations@example.com",
        )
        self.now = datetime(2026, 9, 12, 12, 0, tzinfo=dt_timezone.utc)

    def _source(self, *, age_days=8, findings=None, filename="held-source.835"):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename=filename,
            stored_filename=filename,
            status="ARCHIVED",
            held_claims_count=1,
            conversion_findings=findings or [{
                "rule_code": "MP003",
                "severity": "HOLD",
                "claim_index": "1",
                "claim_number": "CLAIM100",
                "reason": "Payment exceeds the derived covered amount.",
            }],
        )
        held_since = self.now - timedelta(days=age_days)
        EDI835File.objects.filter(id=source.id).update(
            uploaded_at=held_since,
            processing_completed_at=held_since,
        )
        source.refresh_from_db()
        return source

    def _pushed_claim(self, claim_number="CLAIM100", *, sent_at=None):
        sent_at = sent_at or (self.now - timedelta(days=1))
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="resolved-source.835",
            stored_filename="resolved-source.835",
            status="ARCHIVED",
        )
        mir_file = MIRFile.objects.create(
            source_835=source,
            client=self.client_record,
            mir_filename="RESOLVED.MIR",
            file_content="test",
            file_hash="a" * 64,
            file_size=4,
            claim_count=1,
            physical_row_count=1,
            service_count=0,
            status="PUSHED",
        )
        MIRFile.objects.filter(id=mir_file.id).update(updated_at=sent_at)
        mir_file.refresh_from_db()
        MIRClaim.objects.create(
            mir_file=mir_file,
            claim_sequence=1,
            claim_control_number=claim_number,
            header_raw=" " * 334,
        )
        return mir_file

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_overdue_claim_emails_once_per_day_and_again_next_day(self, send_email, _users):
        source = self._source()

        first = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(first, {"emailed_claims": 1, "emails_sent": 1, "email_failures": 0})
        self.assertEqual(send_email.call_count, 1)
        self.assertIn("Daily Alert", send_email.call_args.args[1])
        self.assertIn("CLAIM100", send_email.call_args.args[2])
        self.assertIn("MP003", send_email.call_args.args[2])

        source.refresh_from_db()
        finding = source.conversion_findings[0]
        self.assertTrue(finding.get(ALERT_FIELD))
        self.assertEqual(finding.get(ALERT_COUNT_FIELD), 1)
        self.assertTrue(finding.get(ALERT_LAST_FIELD))

        same_day = send_overdue_nonduplicate_hold_alerts(now=self.now + timedelta(hours=1))
        self.assertEqual(same_day["emailed_claims"], 0)
        self.assertEqual(send_email.call_count, 1)

        next_day = send_overdue_nonduplicate_hold_alerts(now=self.now + timedelta(days=1))
        self.assertEqual(next_day["emailed_claims"], 1)
        self.assertEqual(send_email.call_count, 2)
        source.refresh_from_db()
        self.assertEqual(source.conversion_findings[0].get(ALERT_COUNT_FIELD), 2)

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_alerts_stop_after_seven_daily_emails(self, send_email, _users):
        source = self._source()

        for day in range(7):
            result = send_overdue_nonduplicate_hold_alerts(now=self.now + timedelta(days=day))
            self.assertEqual(result["emailed_claims"], 1)

        eighth_day = send_overdue_nonduplicate_hold_alerts(now=self.now + timedelta(days=7))
        self.assertEqual(eighth_day["emailed_claims"], 0)
        self.assertEqual(send_email.call_count, 7)

        source.refresh_from_db()
        self.assertEqual(source.conversion_findings[0].get(ALERT_COUNT_FIELD), 7)

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_duplicate_holds_are_excluded_and_exactly_seven_days_is_not_overdue(self, send_email, _users):
        self._source(age_days=8, findings=[{
            "rule_code": "DUPLICATE_RECENT_MIR",
            "severity": "HOLD",
            "claim_index": "1",
            "claim_number": "DUP100",
            "reason": "Recent duplicate.",
        }], filename="duplicate.835")
        self._source(age_days=7, findings=[{
            "rule_code": "MP013",
            "severity": "HOLD",
            "claim_index": "1",
            "claim_number": "EXACT700",
            "reason": "Non-duplicate hold.",
        }], filename="exact-seven.835")

        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result["emailed_claims"], 0)
        send_email.assert_not_called()

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=False)
    def test_failed_email_is_not_counted_so_worker_can_retry(self, _send_email, _users):
        source = self._source()
        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result["email_failures"], 1)

        source.refresh_from_db()
        finding = source.conversion_findings[0]
        self.assertFalse(finding.get(ALERT_FIELD))
        self.assertFalse(finding.get(ALERT_COUNT_FIELD))

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_later_pushed_mir_marks_claim_resolved_and_stops_email(self, send_email, _users):
        source = self._source()
        self._pushed_claim("CLAIM100", sent_at=self.now - timedelta(hours=1))

        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result["emailed_claims"], 0)
        send_email.assert_not_called()

        source.refresh_from_db()
        finding = source.conversion_findings[0]
        self.assertEqual(finding.get(RESOLUTION_STATUS_FIELD), "RESOLVED")
        self.assertEqual(finding.get(RESOLVED_MIR_FIELD), "RESOLVED.MIR")
        self.assertEqual(source.held_claims_count, 0)
