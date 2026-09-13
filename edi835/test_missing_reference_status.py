from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

from django.test import TestCase

from accounts.models import Client
from edi835.alert_models import ClaimAlertEmail
from edi835.missing_reference_views import missing_reference_status_rows
from edi835.models import (
    EDI835File,
    EDI837Claim,
    EDI837File,
    MIRClaim,
    MIRFile,
    RECONClaim,
    RECONFile,
)


class MissingReferenceStatusTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Missing Files Client",
            client_code="MISS-FILES",
            email="operations@example.com",
        )
        self.user = SimpleNamespace(
            client_id=self.client_record.id,
            is_authenticated=True,
            is_staff=False,
        )
        self.sent_at = datetime(2026, 9, 13, 8, 23, 55, tzinfo=dt_timezone.utc)

    def _pushed_claim(self, claim_number="STATUS100"):
        source = EDI835File.objects.create(
            client=self.client_record,
            original_filename=f"{claim_number}.835",
            stored_filename=f"{claim_number}.835",
            status="ARCHIVED",
        )
        came_in_at = self.sent_at - timedelta(hours=2)
        EDI835File.objects.filter(id=source.id).update(uploaded_at=came_in_at)
        source.refresh_from_db()
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
        MIRFile.objects.filter(id=mir.id).update(updated_at=self.sent_at)
        mir.refresh_from_db()
        MIRClaim.objects.create(
            mir_file=mir,
            claim_sequence=1,
            claim_control_number=f"{claim_number:<17}REF001",
            header_raw=" " * 334,
        )
        return source, mir

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

    def test_status_lists_missing_sources_next_email_and_email_counter(self):
        source, _mir = self._pushed_claim()
        before_deadline = datetime(2026, 9, 20, 20, 0, tzinfo=dt_timezone.utc)

        rows = missing_reference_status_rows(self.user, now=before_deadline)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["claim_number"], "STATUS100")
        self.assertEqual(row["missing_in"], ["837", "RECON"])
        self.assertEqual(row["email_count"], 0)
        self.assertEqual(
            datetime.fromisoformat(row["next_email_at"]).astimezone(dt_timezone.utc),
            datetime(2026, 9, 20, 21, 30, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(row["source_835_filename"], source.original_filename)

        sent_time = datetime(2026, 9, 20, 21, 30, tzinfo=dt_timezone.utc)
        ClaimAlertEmail.objects.create(
            client=self.client_record,
            category="MISSING_REFERENCE",
            alert_date=date(2026, 9, 20),
            status="SENT",
            subject="Missing reference",
            recipients=["operations@example.com"],
            claims=[{"claim_number": "STATUS100"}],
            sent_at=sent_time,
        )
        after_email = sent_time + timedelta(minutes=10)
        row = missing_reference_status_rows(self.user, now=after_email)[0]
        self.assertEqual(row["email_count"], 1)
        self.assertEqual(row["last_email_sent_at"], sent_time.isoformat())
        self.assertEqual(
            datetime.fromisoformat(row["next_email_at"]).astimezone(dt_timezone.utc),
            datetime(2026, 9, 21, 21, 30, tzinfo=dt_timezone.utc),
        )

        self._add_837("STATUS100")
        row = missing_reference_status_rows(self.user, now=after_email)[0]
        self.assertEqual(row["missing_in"], ["RECON"])

        self._add_recon("STATUS100")
        self.assertEqual(missing_reference_status_rows(self.user, now=after_email), [])
