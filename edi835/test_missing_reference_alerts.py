from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase

from accounts.models import Client
from edi835.alert_models import ClaimAlertEmail
from edi835.missing_reference_alerts import (
    missing_reference_eligible_at,
    send_missing_reference_alerts,
)
from edi835.models import (
    EDI835File,
    EDI837Claim,
    EDI837File,
    MIRClaim,
    MIRFile,
    RECONClaim,
    RECONFile,
)


class MissingReferenceAlertTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Reference Alert Client",
            client_code="REF-ALERT",
            email="operations@example.com",
        )

    def _pushed_claim(self, claim_number, sent_at):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename=f"{claim_number}.835",
            stored_filename=f"{claim_number}.835",
            status="ARCHIVED",
        )
        mir = MIRFile.objects.create(
            source_835=source,
            client=self.client_record,
            mir_filename=f"{claim_number}.MIR",
            file_content="test",
            file_hash=(claim_number.lower() + "0" * 64)[:64],
            file_size=4,
            claim_count=1,
            physical_row_count=1,
            service_count=0,
            status="PUSHED",
        )
        MIRFile.objects.filter(id=mir.id).update(updated_at=sent_at)
        mir.refresh_from_db()
        MIRClaim.objects.create(
            mir_file=mir,
            claim_sequence=1,
            claim_control_number=f"{claim_number:<17}REF001",
            header_raw=" " * 334,
        )
        return mir

    def _add_837(self, claim_number):
        edi_file = EDI837File.objects.create(
            client=self.client_record,
            original_filename="reference.837",
            stored_filename="reference.837",
            file_content="test",
            file_hash="8" * 64,
            file_size=4,
            status="PROCESSED",
        )
        EDI837Claim.objects.create(
            edi_file=edi_file,
            client=self.client_record,
            claim_sequence=1,
            claim_control_number=claim_number,
            highmark_claim_number=claim_number,
        )

    def _add_recon(self, claim_number):
        recon_file = RECONFile.objects.create(
            client=self.client_record,
            original_filename="reference.recon",
            stored_filename="reference.recon",
            file_content="test",
            file_hash="9" * 64,
            file_size=4,
            file_kind="RECON",
            status="PROCESSED",
        )
        RECONClaim.objects.create(
            recon_file=recon_file,
            client=self.client_record,
            claim_sequence=1,
            claim_control_number=claim_number,
        )

    def test_day_seven_eligibility_is_530_pm_eastern(self):
        sent_at = datetime(2026, 9, 13, 8, 23, 55, tzinfo=dt_timezone.utc)
        self.assertEqual(
            missing_reference_eligible_at(sent_at).astimezone(dt_timezone.utc),
            datetime(2026, 9, 20, 21, 30, tzinfo=dt_timezone.utc),
        )

    @patch("edi835.missing_reference_alerts.alert_recipients", return_value=["operations@example.com"])
    @patch("edi835.missing_reference_alerts.send_client_email", return_value=True)
    def test_single_consolidated_email_at_530_lists_all_missing_claims(self, send_email, _recipients):
        sent_at = datetime(2026, 9, 13, 8, 23, 55, tzinfo=dt_timezone.utc)
        self._pushed_claim("MISSBOTH", sent_at)
        self._pushed_claim("MISSONLYRECON", sent_at + timedelta(minutes=1))
        self._add_837("MISSONLYRECON")

        before = datetime(2026, 9, 20, 21, 29, 59, tzinfo=dt_timezone.utc)
        result = send_missing_reference_alerts(now=before)
        self.assertEqual(result["emails_sent"], 0)
        send_email.assert_not_called()

        at_deadline = datetime(2026, 9, 20, 21, 30, tzinfo=dt_timezone.utc)
        result = send_missing_reference_alerts(now=at_deadline)
        self.assertEqual(result["emails_sent"], 1)
        self.assertEqual(result["emailed_claims"], 2)
        self.assertEqual(send_email.call_count, 1)
        html = send_email.call_args.args[2]
        self.assertIn("MISSBOTH", html)
        self.assertIn("MISSONLYRECON", html)
        self.assertIn("837 and RECON", html)
        self.assertIn("RECON", html)

        audit = ClaimAlertEmail.objects.get(category="MISSING_REFERENCE")
        self.assertEqual(audit.status, "SENT")
        self.assertEqual(len(audit.claims), 2)
        missing = {item["claim_number"]: item["missing_in"] for item in audit.claims}
        self.assertEqual(missing["MISSBOTH"], ["837", "RECON"])
        self.assertEqual(missing["MISSONLYRECON"], ["RECON"])

        same_day = send_missing_reference_alerts(now=at_deadline + timedelta(hours=2))
        self.assertEqual(same_day["emails_sent"], 0)
        self.assertEqual(send_email.call_count, 1)

    @patch("edi835.missing_reference_alerts.alert_recipients", return_value=["operations@example.com"])
    @patch("edi835.missing_reference_alerts.send_client_email", return_value=True)
    def test_email_repeats_daily_until_both_837_and_recon_exist(self, send_email, _recipients):
        sent_at = datetime(2026, 9, 13, 8, 23, 55, tzinfo=dt_timezone.utc)
        self._pushed_claim("DAILY100", sent_at)
        day7 = datetime(2026, 9, 20, 21, 30, tzinfo=dt_timezone.utc)

        first = send_missing_reference_alerts(now=day7)
        self.assertEqual(first["emails_sent"], 1)

        self._add_837("DAILY100")
        second = send_missing_reference_alerts(now=day7 + timedelta(days=1))
        self.assertEqual(second["emails_sent"], 1)
        latest = ClaimAlertEmail.objects.order_by("-sent_at").first()
        self.assertEqual(latest.claims[0]["missing_in"], ["RECON"])

        self._add_recon("DAILY100")
        third = send_missing_reference_alerts(now=day7 + timedelta(days=2))
        self.assertEqual(third["emails_sent"], 0)
        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(ClaimAlertEmail.objects.filter(category="MISSING_REFERENCE", status="SENT").count(), 2)
