from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase

from accounts.models import Client
from edi835.long_hold_alerts import ALERT_FIELD, send_overdue_nonduplicate_hold_alerts
from edi835.models import EDI835File


class LongHoldAlertTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Long Hold Client",
            client_code="LONG-HOLD",
            email="operations@example.com",
        )
        self.now = datetime(2026, 9, 12, 12, 0, tzinfo=dt_timezone.utc)

    def _source(self, *, age_days=8, findings=None):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename="held-source.835",
            stored_filename="held-source.835",
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

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_overdue_nonduplicate_claim_is_emailed_once(self, send_email, _users):
        source = self._source()

        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result, {"emailed_claims": 1, "emails_sent": 1, "email_failures": 0})
        send_email.assert_called_once()
        subject = send_email.call_args.args[1]
        html = send_email.call_args.args[2]
        self.assertIn("Held More Than 7 Days", subject)
        self.assertIn("CLAIM100", html)
        self.assertIn("held-source.835", html)
        self.assertIn("MP003", html)

        source.refresh_from_db()
        self.assertTrue(source.conversion_findings[0].get(ALERT_FIELD))

        again = send_overdue_nonduplicate_hold_alerts(now=self.now + timedelta(hours=1))
        self.assertEqual(again["emailed_claims"], 0)
        self.assertEqual(send_email.call_count, 1)

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=True)
    def test_duplicate_holds_are_excluded_and_exactly_seven_days_is_not_overdue(self, send_email, _users):
        self._source(age_days=8, findings=[{
            "rule_code": "DUPLICATE_RECENT_MIR",
            "severity": "HOLD",
            "claim_index": "1",
            "claim_number": "DUP100",
            "reason": "Recent duplicate.",
        }])
        self._source(age_days=7, findings=[{
            "rule_code": "MP013",
            "severity": "HOLD",
            "claim_index": "1",
            "claim_number": "EXACT700",
            "reason": "Non-duplicate hold.",
        }])

        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result["emailed_claims"], 0)
        send_email.assert_not_called()

    @patch("edi835.long_hold_alerts.get_client_users", return_value=[])
    @patch("edi835.long_hold_alerts.send_client_email", return_value=False)
    def test_failed_email_is_not_marked_so_worker_can_retry(self, _send_email, _users):
        source = self._source()
        result = send_overdue_nonduplicate_hold_alerts(now=self.now)
        self.assertEqual(result["email_failures"], 1)

        source.refresh_from_db()
        self.assertFalse(source.conversion_findings[0].get(ALERT_FIELD))
