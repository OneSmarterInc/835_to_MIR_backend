"""Regression coverage for client period claim comparison and data isolation."""
from datetime import date, datetime, time
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from edi835.client_portal_comparison import comparison, period_rows, sorted_rows
from edi835.client_portal_period import resolve_period
from edi835.models import (
    EDI835File, EDI835Claim, MIRFile, MIRClaim,
    MIRClaimChunk, MIRServiceLine, RECONFile, RECONClaim,
)

EASTERN = ZoneInfo("America/New_York")


def moment(day):
    return datetime.combine(day, time(12), tzinfo=EASTERN)


class ClientClaimComparisonTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(
            name="Hospital A", client_code="CMP-TEST-A",
            email="a@comparison.example", timezone="America/New_York",
        )
        self.other = Client.objects.create(
            name="Hospital B", client_code="CMP-TEST-B",
            email="b@comparison.example", timezone="America/New_York",
        )
        self.viewer = User.objects.create_user(
            email="reader@comparison.example", name="Reader",
            mobile="+12125550343", password="NotARealPassword123!",
            client=self.client, client_role="CLIENT_USER",
        )
        self.record("CLM100", self.client, date(2026, 10, 12),
                    billed="100", mir_billed="100", mir_paid="80", recon_paid="80")
        self.record("CLM200", self.client, date(2026, 10, 14),
                    billed="130", mir_billed="120", mir_paid="75", recon_paid="60")
        self.record("CLM300", self.client, date(2026, 10, 15),
                    billed="60")
        self.record("OLD400", self.client, date(2026, 9, 29),
                    billed="900", mir_billed="850", mir_paid="700", recon_paid="600")
        self.record("CLM100", self.other, date(2026, 10, 12),
                    billed="9999", mir_billed="9999", mir_paid="9000", recon_paid="9000")

    def record(self, key, client, day, *, billed, mir_billed=None,
               mir_paid=None, recon_paid=None):
        source = EDI835File.objects.create(
            client=client, original_filename=key + ".835",
            stored_filename=key + ".835",
            claims_count=1, status="ARCHIVED",
        )
        EDI835File.objects.filter(pk=source.pk).update(uploaded_at=moment(day))
        EDI835Claim.objects.create(
            edi_file=source, claim_sequence=1,
            highmark_claim_number=key, total_charge_amount=Decimal(billed),
            paid_amount=Decimal(mir_paid or 0),
        )
        if mir_billed is not None:
            mir = MIRFile.objects.create(
                source_835=source, client=client, mir_filename=key + ".mir",
                file_content="MIR", file_hash=key,
                converted_at=moment(day), claim_count=1,
            )
            claim = MIRClaim.objects.create(
                mir_file=mir, claim_sequence=1,
                claim_control_number=key, header_raw="H" * 334,
                service_count=1,
            )
            chunk = MIRClaimChunk.objects.create(
                mir_claim=claim, chunk_number=1,
                physical_row_number=1, raw_row="ROW",
                row_length=3, services_in_chunk=1,
            )
            MIRServiceLine.objects.create(
                mir_claim=claim, mir_chunk=chunk,
                service_sequence=1, chunk_service_sequence=1,
                service_raw="S" * 303,
                charge_amount=Decimal(mir_billed),
                paid_amount=Decimal(mir_paid),
            )
        if recon_paid is not None:
            recon = RECONFile.objects.create(
                client=client, original_filename=key + ".recon",
                stored_filename=key + ".recon",
                file_content="DATA", file_hash=key,
                status="PROCESSED", file_kind="RECON",
            )
            RECONFile.objects.filter(pk=recon.pk).update(uploaded_at=moment(day))
            RECONClaim.objects.create(
                recon_file=recon, client=client, claim_sequence=1,
                claim_control_number=key,
                paid_amount=Decimal(recon_paid),
            )

    def fetch(self, **params):
        request = self.factory.get("/edi835/api/client-portal/comparison/", params)
        request.user = self.viewer
        response = comparison(request)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_custom_period_has_independent_true_money_sources(self):
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"},
            sort="claim", direction="asc",
        )
        self.assertEqual(response["count"], 3)
        by_claim = {r["claim"]: r for r in response["rows"]}
        self.assertEqual(by_claim["CLM100"]["status"], "Clear")
        self.assertEqual(by_claim["CLM100"]["mismatch_amount"], "0.00")
        self.assertEqual(by_claim["CLM100"]["billed_835"], "100.00")
        self.assertEqual(by_claim["CLM100"]["billed_mir"], "100.00")
        self.assertEqual(by_claim["CLM100"]["paid_mir"], "80.00")
        self.assertEqual(by_claim["CLM100"]["recon_amount"], "80.00")

        self.assertEqual(by_claim["CLM200"]["status"], "Mismatch")
        self.assertEqual(by_claim["CLM200"]["mismatch_amount"], "25.00")
        self.assertEqual(by_claim["CLM300"]["status"], "Mismatch")
        self.assertIsNone(by_claim["CLM300"]["mismatch_amount"])
        self.assertIsNone(by_claim["CLM300"]["billed_mir"])
        self.assertIsNone(by_claim["CLM300"]["recon_amount"])
        self.assertIn("No MIR", by_claim["CLM300"]["reason"])
        self.assertEqual(response["summary"], {
            "clear": 1, "mismatch": 2, "missing_streams": 1,
            "confirmed_mismatch_amount": "25.00",
        })

    def test_date_filter_keeps_old_claims_out(self):
        response = self.fetch(
            period="custom", **{"from": "2026-09-29", "to": "2026-09-29"}
        )
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["rows"][0]["claim"], "OLD400")

    def test_monthly_and_weekly_boundaries(self):
        current = resolve_period({"period": "weekly"}, self.client, today=date(2026, 10, 15))
        rows = period_rows(self.client, current)
        self.assertEqual({r["claim"] for r in rows}, {"CLM100", "CLM200", "CLM300"})
        monthly = resolve_period({"period": "monthly"}, self.client, today=date(2026, 10, 15))
        self.assertEqual({r["claim"] for r in period_rows(self.client, monthly)},
                         {"CLM100", "CLM200", "CLM300"})

    def test_sort_every_supported_column(self):
        window = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        for column in ("claim", "date", "billed_835", "billed_mir",
                       "paid_mir", "recon_amount", "mismatch_amount", "status"):
            ascending = self.fetch(**window, sort=column, direction="asc")
            descending = self.fetch(**window, sort=column, direction="desc")
            self.assertEqual(ascending["count"], 3)
            self.assertEqual(descending["count"], 3)
            self.assertEqual({r["claim"] for r in ascending["rows"]},
                             {r["claim"] for r in descending["rows"]})
        # Unknown monetary values are last in either direction.
        self.assertEqual(self.fetch(**window, sort="billed_mir", direction="asc")
                         ["rows"][-1]["claim"], "CLM300")
        self.assertEqual(self.fetch(**window, sort="billed_mir", direction="desc")
                         ["rows"][-1]["claim"], "CLM300")

    def test_sort_precedes_pagination_and_rejects_unapproved_columns(self):
        params = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15",
                  "sort": "billed_835", "direction": "desc", "page_size": "1"}
        first = self.fetch(**params, page=1)
        second = self.fetch(**params, page=2)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(first["rows"][0]["claim"], "CLM200")
        self.assertEqual(second["rows"][0]["claim"], "CLM100")
        req = self.factory.get("/edi835/api/client-portal/comparison/", {
            **params, "sort": "password"
        })
        req.user = self.viewer
        self.assertEqual(comparison(req).status_code, 400)

    def test_all_keeps_other_tenant_out(self):
        response = self.fetch(period="all", page_size=20)
        self.assertEqual(response["count"], 4)
        self.assertNotIn("Hospital B", json.dumps(response))
        self.assertEqual(sum(Decimal(r["billed_835"]) for r in response["rows"]),
                         Decimal("1190"))

    def test_duplicate_claim_identifier_is_not_marked_clear(self):
        # Reusing the same claim control number in two 835 remittances must not
        # look reconciled solely because aggregate differences cancel.
        self.record("CLM100", self.client, date(2026, 10, 15),
                    billed="100", mir_billed="100", mir_paid="80", recon_paid="80")
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"},
            sort="claim", direction="asc",
        )
        row = next(r for r in response["rows"] if r["claim"] == "CLM100")
        self.assertEqual(row["status"], "Mismatch")
        self.assertIn("Repeated claim identifier", row["reason"])
        self.assertEqual(row["billed_835"], "200.00")
        self.assertEqual(row["billed_mir"], "200.00")

    def test_zero_is_a_real_amount_not_the_same_as_missing(self):
        self.record("ZERO500", self.client, date(2026, 10, 15),
                    billed="0", mir_billed="0", mir_paid="0", recon_paid="0")
        response = self.fetch(
            period="custom", **{"from": "2026-10-15", "to": "2026-10-15"},
            sort="claim", direction="asc",
        )
        row = next(r for r in response["rows"] if r["claim"] == "ZERO500")
        self.assertEqual(row["status"], "Clear")
        self.assertEqual(row["recon_amount"], "0.00")
        self.assertEqual(row["mismatch_amount"], "0.00")

    def test_mir_extended_control_number_matches_only_its_source_835(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM200.835")
        mir = MIRClaim.objects.get(mir_file__source_835=source)
        recon = RECONClaim.objects.get(
            recon_file__client=self.client, claim_control_number="CLM200",
        )
        mir.claim_control_number = "CLM200SFX7"
        mir.save(update_fields=["claim_control_number"])
        recon.claim_control_number = "CLM200SFX7"
        recon.save(update_fields=["claim_control_number"])
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"}
        )
        row = next(r for r in response["rows"] if r["claim"] == "CLM200")
        self.assertEqual(row["billed_mir"], "120.00")
        self.assertEqual(row["recon_amount"], "60.00")
        self.assertEqual(row["mismatch_amount"], "25.00")

    def test_mir_without_service_rows_is_not_a_zero_dollar_clear(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM100.835")
        mir = MIRClaim.objects.get(mir_file__source_835=source)
        MIRServiceLine.objects.filter(mir_claim=mir).delete()
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"}
        )
        row = next(r for r in response["rows"] if r["claim"] == "CLM100")
        self.assertEqual(row["status"], "Mismatch")
        self.assertIsNone(row["billed_mir"])
        self.assertIsNone(row["paid_mir"])
        self.assertIn("MIR service amounts", row["reason"])
