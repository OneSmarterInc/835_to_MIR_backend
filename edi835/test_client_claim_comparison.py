"""Regression coverage for client period claim comparison and data isolation."""
from datetime import date, datetime, time
from decimal import Decimal
import csv
import io
import json
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from edi835.client_portal_comparison import comparison, period_rows, sorted_rows, csv_safe_text
from edi835.client_portal_period import resolve_period
from edi835.models import (
    EDI835File, EDI835Claim, MIRFile, MIRClaim,
    MIRClaimChunk, MIRServiceLine, RECONFile, RECONClaim,
    ClientPortalAction,
)

EASTERN = ZoneInfo("America/New_York")


def moment(day):
    return datetime.combine(day, time(12), tzinfo=EASTERN)


class ClientClaimComparisonTests(TestCase):
    def test_age_filter_boundaries_pagination_and_csv(self):
        period = resolve_period({"period": "all"}, self.client)
        template = period_rows(self.client, period)[0]
        ages = [0, 9, 10, 19, 20, 29, 30, 90, None]
        rows = [{**template, "key": str(index), "claim": f"AGE{index}",
                 "claim_id": None, "waiting_days": age} for index, age in enumerate(ages)]
        with patch("edi835.client_portal_comparison.period_rows", return_value=rows):
            for band, expected in (("all", ages), ("0-10", [0, 9]),
                                   ("10-20", [10, 19]), ("20-30", [20, 29]),
                                   ("30-plus", [30, 90])):
                with self.subTest(band=band):
                    result = self.fetch(period="all", age_band=band, sort="waiting_days", direction="asc")
                    self.assertEqual([row["waiting_days"] for row in result["rows"]], expected)
                    self.assertEqual(result["count"], len(expected))
                    counts = {"0-10": 0, "10-20": 0, "20-30": 0, "30-plus": 0}
                    for value in expected:
                        if value is not None:
                            counts["0-10" if value < 10 else "10-20" if value < 20 else "20-30" if value < 30 else "30-plus"] += 1
                    self.assertEqual(result["age_summary"], {
                        "counts": counts, "unknown": expected.count(None), "total": len(expected),
                    })
                    page = self.fetch(period="all", age_band=band, page_size=1, page=2,
                                      sort="waiting_days", direction="asc")
                    self.assertEqual(page["pages"], len(expected))
                    self.assertEqual(page["rows"][0]["waiting_days"], expected[1])
                    self.assertEqual(page["age_summary"], result["age_summary"])
                    request = self.factory.get("/comparison/", {"period": "all", "age_band": band,
                        "format": "csv", "page_size": 1, "sort": "waiting_days", "direction": "asc"})
                    request.user = self.viewer
                    response = comparison(request)
                    content = b"".join(response.streaming_content).decode("utf-8-sig")
                    exported = list(csv.DictReader(io.StringIO(content)))
                    self.assertEqual([row["Days waiting"] for row in exported],
                                     ["" if age is None else str(age) for age in expected])

    def test_invalid_age_filter_is_rejected(self):
        request = self.factory.get("/comparison/", {"period": "all", "age_band": "-10"})
        request.user = self.viewer
        self.assertEqual(comparison(request).status_code, 400)

    @patch("edi835.client_portal_period.timezone.now", return_value=moment(date(2026, 10, 16)))
    def test_age_chart_includes_nonheld_fifteen_day_claims_and_matches_filters(self, _now):
        self.record("FIFTEEN", self.client, date(2026, 10, 1), billed="100")
        self.record("FIFTEEN", self.other, date(2026, 10, 1), billed="9999")
        # A separate recent hold must not define the chart's population.
        EDI835File.objects.filter(client=self.client, original_filename="CLM300.835").update(
            held_claims_count=1, conversion_findings=[{"severity": "HOLD", "claim_index": 1}],
        )
        result = self.fetch(period="monthly", page_size=1)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["rows"][0]["waiting_days"], 15)
        self.assertFalse(result["rows"][0]["is_held"])
        self.assertEqual(result["age_summary"], {
            "counts": {"0-10": 3, "10-20": 1, "20-30": 0, "30-plus": 0},
            "unknown": 0, "total": 4,
        })
        filtered = self.fetch(period="monthly", payer_status="denied", age_band="10-20")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["age_summary"]["counts"]["10-20"], 1)
        empty = self.fetch(period="daily", age_band="10-20")
        self.assertEqual(empty["age_summary"]["total"], 0)
        self.assertEqual(sum(empty["age_summary"]["counts"].values()), 0)

    def test_age_summary_is_not_limited_to_200_preview_rows(self):
        template = period_rows(self.client, resolve_period({"period": "all"}, self.client))[0]
        rows = [{**template, "key": str(index), "claim_id": None, "waiting_days": 15,
                 "payer_status": "No payment" if index < 230 else "Paid in full"}
                for index in range(250)]
        with patch("edi835.client_portal_comparison.period_rows", return_value=rows):
            result = self.fetch(period="all", payer_status="denied", page_size=10, page=2)
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual(result["age_summary"]["total"], 230)
        self.assertEqual(result["age_summary"]["counts"]["10-20"], 230)

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
               mir_paid=None, recon_paid=None, paid_835=None):
        source = EDI835File.objects.create(
            client=client, original_filename=key + ".835",
            stored_filename=key + ".835",
            claims_count=1, status="ARCHIVED",
        )
        EDI835File.objects.filter(pk=source.pk).update(uploaded_at=moment(day))
        EDI835Claim.objects.create(
            edi_file=source, claim_sequence=1,
            highmark_claim_number=key, total_charge_amount=Decimal(billed),
            paid_amount=Decimal(paid_835 if paid_835 is not None else mir_paid or 0),
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
            "billed_gap_amount": "70.00",
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
                       "paid_835", "paid_mir", "recon_amount", "mismatch_amount", "status"):
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

    def test_each_stream_uses_its_own_date_and_missing_is_not_zero(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM100.835")
        MIRFile.objects.filter(source_835=source).update(
            converted_at=moment(date(2026, 9, 30))
        )
        RECONFile.objects.filter(client=self.client, original_filename="CLM100.recon").update(
            uploaded_at=moment(date(2026, 9, 30))
        )
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"},
            sort="claim", direction="asc",
        )
        row = next(r for r in response["rows"] if r["claim"] == "CLM100")
        self.assertEqual(row["billed_835"], "100.00")
        self.assertIsNone(row["billed_mir"])
        self.assertIsNone(row["recon_amount"])
        self.assertIsNone(row["mismatch_amount"])
        self.assertEqual(row["status"], "Mismatch")

    def test_non_client_user_cannot_see_comparison(self):
        admin = User.objects.create_user(
            email="admin@comparison.example", name="Client Admin",
            mobile="+12125550344", password="NotARealPassword123!",
            client=self.client, client_role="CLIENT_ADMIN",
        )
        request = self.factory.get("/edi835/api/client-portal/comparison/?period=all")
        request.user = admin
        response = comparison(request)
        self.assertEqual(response.status_code, 403)

    def test_payer_status_filter_recalculates_rows_summary_and_financial_cards(self):
        window = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        less = self.fetch(**window, payer_status="less")
        self.assertEqual(less["count"], 2)
        self.assertEqual(less["financial_totals"]["billed_835_amount"], "230.00")
        self.assertEqual(less["financial_totals"]["billed_mir_amount"], "220.00")
        self.assertEqual(less["financial_totals"]["recon_amount"], "140.00")
        self.assertEqual(less["summary"]["confirmed_mismatch_amount"], "25.00")
        self.assertEqual(less["summary"]["billed_gap_amount"], "10.00")
        denied = self.fetch(**window, payer_status="denied")
        self.assertEqual(denied["count"], 1)
        self.assertEqual(denied["rows"][0]["claim"], "CLM300")
        self.assertEqual(denied["financial_totals"]["billed_835_amount"], "60.00")
        self.assertEqual(denied["financial_totals"]["billed_mir_services"], 0)
        paid = self.fetch(**window, payer_status="paid")
        self.assertEqual(paid["count"], 0)
        self.assertEqual(paid["summary"]["confirmed_mismatch_amount"], "0.00")
        self.assertEqual(paid["summary"]["billed_gap_amount"], "0.00")
        self.assertEqual(paid["financial_totals"]["billed_835_claims"], 0)

    def csv_download(self, **params):
        request = self.factory.get(
            "/edi835/api/client-portal/comparison/",
            {**params, "format": "csv"},
        )
        request.user = self.viewer
        response = comparison(request)
        self.assertEqual(response.status_code, 200, getattr(response, "content", b""))
        self.assertTrue(response.streaming)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        raw = b"".join(response.streaming_content).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(raw))), response

    def test_csv_export_contains_every_filtered_row_not_only_current_page(self):
        window = {
            "period": "custom", "from": "2026-10-12", "to": "2026-10-15",
            "sort": "billed_835", "direction": "desc", "page_size": 1, "page": 2,
        }
        rows, response = self.csv_download(**window)
        self.assertEqual([row["Claim"] for row in rows], ["CLM200", "CLM100", "CLM300"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Billed in 835"], "130.00")
        self.assertEqual(rows[0]["Mismatch amount"], "25.00")
        self.assertEqual(rows[0]["835/MIR status"], "Billed differs")
        self.assertEqual(rows[0]["MIR/Recon status"], "Paid differs")
        self.assertEqual(rows[0]["Paid 835"], "75.00")
        self.assertEqual(rows[0]["Billed MIR"], "120.00")
        self.assertEqual(rows[0]["Latest note / action"], "")
        self.assertNotIn("Status", rows[0])
        self.assertNotIn("Issue / reason", rows[0])
        self.assertNotIn("Latest activity", rows[0])
        self.assertEqual(rows[0]["Payer status"], "Paid less than billed")
        self.assertEqual(rows[-1]["Amount in Recon"], "")
        self.assertIn("custom_20261012-20261015", response["Content-Disposition"])

    def test_csv_includes_both_like_for_like_pairs_and_separate_recon_status(self):
        rows, response = self.csv_download(period="all", sort="waiting_days", direction="desc")
        self.assertTrue(rows)
        self.assertEqual(
            list(rows[0]),
            [
                "Days waiting", "Claim", "Internal claim", "Billed in 835",
                "Billed MIR", "Paid 835", "Paid MIR", "Amount in Recon", "Mismatch amount",
                "835/MIR status", "MIR/Recon status", "Latest note / action", "Payer status",
            ],
        )
        self.assertNotIn("Latest activity", rows[0])
        self.assertNotIn("Issue / reason", rows[0])

    def test_issue_labels_are_short_and_specific(self):
        params = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        result = self.fetch(**params, sort="claim", direction="asc")
        by_claim = {row["claim"]: row for row in result["rows"]}
        self.assertEqual(by_claim["CLM100"]["issue_label"], "Billed & paid match")
        self.assertEqual(by_claim["CLM100"]["issue_tone"], "clear")
        self.assertEqual(
            by_claim["CLM200"]["issue_label"],
            "Billed differs",
        )
        self.assertEqual(by_claim["CLM200"]["issue_tone"], "error")
        self.assertEqual(
            by_claim["CLM300"]["issue_label"],
            "MIR record missing",
        )
        self.assertEqual(by_claim["CLM300"]["issue_tone"], "error")
        self.assertIn("MIR", by_claim["CLM300"]["issue_detail"])

    def test_new_issue_and_action_columns_sort_across_entire_filtered_period(self):
        window = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        for key in ("issue_label", "latest_note_action", "payer_status"):
            ascending = self.fetch(**window, sort=key, direction="asc")
            descending = self.fetch(**window, sort=key, direction="desc")
            self.assertEqual(ascending["count"], 3)
            self.assertEqual(
                set(row["claim"] for row in ascending["rows"]),
                set(row["claim"] for row in descending["rows"]),
            )
        csv_rows, _ = self.csv_download(**window, sort="issue_label", direction="asc")
        self.assertEqual(len(csv_rows), 3)
        self.assertNotIn("Status", csv_rows[0])
        self.assertEqual(list(csv_rows[0])[-1], "Payer status")


    def test_hold_explanation_is_short_but_full_reason_is_preserved(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM100.835")
        EDI835File.objects.filter(pk=source.pk).update(
            held_claims_count=1,
            conversion_findings=[
                {"severity": "HOLD", "claim_index": "1",
                 "reason": "This claim is held until the four-day waiting period expires."}
            ],
        )
        response = self.fetch(
            period="custom", **{"from": "2026-10-12", "to": "2026-10-15"},
            sort="claim", direction="asc",
        )
        row = next(row for row in response["rows"] if row["claim"] == "CLM100")
        self.assertEqual(row["issue_label"], "Billed & paid match")
        self.assertEqual(row["issue_tone"], "clear")
        self.assertIn("four-day waiting period", row["issue_detail"])
        self.assertNotIn("four-day waiting period", row["issue_label"])

    def test_latest_note_action_combines_short_cell_and_csv_without_status_column(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM100.835")
        claim = EDI835Claim.objects.get(edi_file=source)
        ClientPortalAction.objects.create(
            client=self.client, file=source, claim=claim,
            created_by=self.viewer, created_by_label="Client User",
            kind="NOTE", message="Check payer adjustment",
        )
        window = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        response = self.fetch(**window, sort="latest_note_action", direction="asc")
        row = next(row for row in response["rows"] if row["claim"] == "CLM100")
        self.assertEqual(row["latest_note_action"], "Note added: Check payer adjustment")
        csv_rows, _ = self.csv_download(**window, sort="latest_note_action", direction="asc")
        claim_csv = next(row for row in csv_rows if row["Claim"] == "CLM100")
        self.assertEqual(claim_csv["Latest note / action"], "Note added: Check payer adjustment")
        self.assertNotIn("Status", claim_csv)

    def test_pending_review_request_is_not_hidden_by_newer_note(self):
        source = EDI835File.objects.get(client=self.client, original_filename="CLM100.835")
        claim = EDI835Claim.objects.get(edi_file=source)
        ClientPortalAction.objects.create(
            client=self.client, file=source, claim=claim, created_by=self.viewer,
            created_by_label="Client User", kind="REQUEST_RELEASE",
            message="Please verify adjustment", status="OPEN",
        )
        ClientPortalAction.objects.create(
            client=self.client, file=source, claim=claim, created_by=self.viewer,
            created_by_label="Client User", kind="NOTE",
            message="Call billing first", status="OPEN",
        )
        window = {"period": "custom", "from": "2026-10-12", "to": "2026-10-15"}
        response = self.fetch(**window, sort="latest_note_action", direction="asc")
        row = next(record for record in response["rows"] if record["claim"] == "CLM100")
        self.assertEqual(row["action_status"], "Pending review")
        self.assertEqual(row["latest_note"], "Call billing first")
        self.assertEqual(row["latest_note_action"], "Pending review: Call billing first")
        csv_rows, _ = self.csv_download(**window, sort="latest_note_action", direction="asc")
        exported = next(record for record in csv_rows if record["Claim"] == "CLM100")
        self.assertEqual(exported["Latest note / action"], "Pending review: Call billing first")

    def test_csv_respects_payer_category_and_tenant(self):
        rows, response = self.csv_download(
            period="all", payer_status="denied", sort="claim", direction="asc"
        )
        self.assertEqual([row["Claim"] for row in rows], ["CLM300"])
        self.assertNotIn("9999", str(rows))
        self.assertEqual(rows[0]["Payer status"], "No payment")

    def test_csv_handles_empty_filtered_results(self):
        rows, response = self.csv_download(period="all", payer_status="paid")
        self.assertEqual(rows, [])
        self.assertEqual(response.status_code, 200)

    def test_csv_protects_formulas_but_keeps_negative_money_valid(self):
        for content in ("=2+2", "+SUM(1)", "-3+4", "@FUNC()", "\t=1+1"):
            self.assertTrue(csv_safe_text(content).startswith("'"))
        self.assertEqual(csv_safe_text("CLM100"), "CLM100")
        self.assertEqual(csv_safe_text(""), "")

    def test_client_admin_cannot_download_csv(self):
        admin = User.objects.create_user(
            email="csv-admin@comparison.example", name="Client Admin",
            mobile="+12125550818", password="NotARealPassword123!",
            client=self.client, client_role="CLIENT_ADMIN",
        )
        request = self.factory.get(
            "/edi835/api/client-portal/comparison/",
            {"period": "all", "format": "csv"},
        )
        request.user = admin
        self.assertEqual(comparison(request).status_code, 403)

    def claim_row(self, key):
        return next(row for row in self.fetch(period="all", page_size=100)["rows"]
                    if row["claim"] == key)

    def test_835_paid_difference_is_not_hidden_by_matching_recon(self):
        EDI835Claim.objects.filter(
            edi_file__client=self.client, highmark_claim_number="CLM100",
        ).update(paid_amount=Decimal("79.99"))
        row = self.claim_row("CLM100")
        self.assertEqual(row["paid_835"], "79.99")
        self.assertEqual(row["issue_label"], "Paid differs")
        self.assertEqual(row["issue_tone"], "error")
        self.assertEqual(row["recon_issue_label"], "Paid matches")
        self.assertEqual(row["status"], "Mismatch")
        self.assertEqual(row["mismatch_amount"], "0.01")
        self.assertIn("835 and MIR paid differ", row["reason"])
        self.assertIn("Paid: 835 79.99 / MIR 80.00", row["issue_detail"])
        exported, _ = self.csv_download(period="all")
        exported = next(item for item in exported if item["Claim"] == "CLM100")
        self.assertEqual(exported["835/MIR status"], "Paid differs")
        self.assertEqual(exported["Paid 835"], "79.99")
        self.assertEqual(exported["Paid MIR"], "80.00")

    def test_partial_payment_compares_paid_to_paid_not_billed(self):
        row = self.claim_row("CLM100")
        self.assertEqual(row["billed_835"], "100.00")
        self.assertEqual(row["paid_835"], "80.00")
        self.assertEqual(row["paid_mir"], "80.00")
        self.assertEqual(row["issue_label"], "Billed & paid match")
        self.assertEqual(row["payer_status"], "Paid less than billed")
        self.assertIn("Billed: 835 100.00 / MIR 100.00", row["issue_detail"])

    def test_both_differences_sum_without_cancelling(self):
        self.record("BOTH", self.client, date(2026, 10, 15),
                    billed="100", paid_835="90", mir_billed="110",
                    mir_paid="80", recon_paid="75")
        row = self.claim_row("BOTH")
        self.assertEqual(row["issue_label"], "Billed & paid differ")
        self.assertEqual(row["recon_issue_label"], "Paid differs")
        self.assertEqual(row["mismatch_amount"], "25.00")

    def test_recon_discrepancy_does_not_change_835_mir_status(self):
        RECONClaim.objects.filter(client=self.client, claim_control_number="CLM100").update(
            paid_amount=Decimal("79"),
        )
        row = self.claim_row("CLM100")
        self.assertEqual(row["issue_label"], "Billed & paid match")
        self.assertEqual(row["issue_tone"], "clear")
        self.assertEqual(row["recon_issue_label"], "Paid differs")
        self.assertEqual(row["recon_issue_tone"], "error")
        self.assertEqual(row["status"], "Mismatch")
        self.assertEqual(row["mismatch_amount"], "1.00")
        self.assertNotIn("Recon", row["issue_detail"])

    def test_missing_recon_does_not_change_835_mir_status(self):
        RECONClaim.objects.filter(client=self.client, claim_control_number="CLM100").delete()
        row = self.claim_row("CLM100")
        self.assertEqual(row["issue_label"], "Billed & paid match")
        self.assertEqual(row["recon_issue_label"], "Recon record missing")
        self.assertEqual(row["status"], "Mismatch")
        self.assertIsNone(row["recon_amount"])

    def test_missing_835_is_unknown_even_when_mir_and_recon_match(self):
        EDI835Claim.objects.filter(
            edi_file__client=self.client, highmark_claim_number="CLM100",
        ).delete()
        row = self.claim_row("CLM100")
        self.assertIsNone(row["paid_835"])
        self.assertEqual(row["issue_label"], "835 record missing")
        self.assertEqual(row["recon_issue_label"], "Paid matches")

    def test_missing_and_incomplete_mir_services_cannot_match(self):
        mir = MIRClaim.objects.get(mir_file__client=self.client, claim_control_number="CLM100")
        for service_count, remove_lines in ((2, False), (1, True)):
            with self.subTest(service_count=service_count):
                mir.service_count = service_count
                mir.save(update_fields=["service_count"])
                if remove_lines:
                    mir.service_lines.all().delete()
                row = self.claim_row("CLM100")
                self.assertEqual(row["issue_label"], "MIR amounts missing")
                self.assertEqual(row["recon_issue_label"], "MIR amounts missing")
                self.assertEqual(row["status"], "Mismatch")
                self.assertIsNone(row["mismatch_amount"])

    def test_duplicate_835_with_balanced_aggregate_does_not_confirm_match(self):
        original = EDI835Claim.objects.get(
            edi_file__client=self.client, highmark_claim_number="CLM100",
        )
        original.total_charge_amount = Decimal("50")
        original.paid_amount = Decimal("40")
        original.save(update_fields=["total_charge_amount", "paid_amount"])
        self.record("CLM100", self.client, date(2026, 10, 15), billed="50", paid_835="40")
        row = self.claim_row("CLM100")
        self.assertEqual(row["billed_835"], row["billed_mir"])
        self.assertEqual(row["paid_835"], row["paid_mir"])
        self.assertEqual(row["issue_label"], "Duplicate 835")
        self.assertEqual(row["recon_issue_label"], "Paid matches")
        self.assertEqual(row["status"], "Mismatch")

    def test_duplicate_mir_with_unpriced_occurrence_is_unresolved(self):
        mir = MIRClaim.objects.get(mir_file__client=self.client, claim_control_number="CLM100")
        MIRClaim.objects.create(mir_file=mir.mir_file, claim_sequence=2,
                                claim_control_number="CLM100", header_raw="H" * 334)
        row = self.claim_row("CLM100")
        self.assertEqual(row["issue_label"], "Duplicate MIR · MIR amounts missing")
        self.assertEqual(row["recon_issue_tone"], "error")
        self.assertIsNone(row["mismatch_amount"])

    def test_duplicate_recon_does_not_change_835_mir_status_or_inflate_variance(self):
        recon = RECONClaim.objects.get(client=self.client, claim_control_number="CLM100")
        recon.pk = None
        recon.claim_sequence = 2
        recon.save()
        row = self.claim_row("CLM100")
        self.assertEqual(row["issue_label"], "Billed & paid match")
        self.assertEqual(row["recon_issue_label"], "Duplicate Recon")
        self.assertEqual(row["status"], "Mismatch")
        self.assertEqual(row["mismatch_amount"], "0.00")

    def test_zero_negative_and_large_amounts_keep_decimal_precision(self):
        cases = (
            ("ZERO", "0.00", "0.00", "0.00", "Billed & paid match"),
            ("NEGATIVE", "-100.00", "-80.00", "-80.01", "Paid differs"),
            ("LARGE", "9999999999999.99", "9999999999999.99",
             "9999999999999.98", "Paid differs"),
        )
        for key, billed, paid, mir_paid, label in cases:
            with self.subTest(key=key):
                self.record(key, self.client, date(2026, 10, 15), billed=billed,
                            paid_835=paid, mir_billed=billed, mir_paid=mir_paid,
                            recon_paid=mir_paid)
                row = self.claim_row(key)
                self.assertEqual(row["paid_835"], paid)
                self.assertEqual(row["issue_label"], label)
                self.assertEqual(row["mismatch_amount"], "0.00" if key == "ZERO" else "0.01")
