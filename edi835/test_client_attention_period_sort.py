"""Needs Attention uses only the selected period, sorted longest waiting first."""
import json
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from edi835.client_portal_holds import attention_claims
from edi835.models import EDI835Claim, EDI835File


EASTERN = ZoneInfo("America/New_York")
FROZEN_TIME = datetime(2026, 10, 15, 14, tzinfo=EASTERN)


class AttentionDateFilterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(
            name="Alpha", client_code="ALPHA-MONTH-SORT", email="alpha@time.example",
            timezone="America/New_York",
        )
        other = Client.objects.create(
            name="Beta", client_code="BETA-MONTH-SORT", email="beta@time.example",
            timezone="America/New_York",
        )
        self.viewer = User.objects.create_user(
            email="monthly-viewer@time.example", name="Viewer",
            mobile="+12125550239", password="LongDummyPassword!",
            client=self.client, client_role="CLIENT_USER",
        )
        for client, name, day in (
            (self.client, "previous-month", date(2026, 9, 30)),
            (self.client, "oct-first", date(2026, 10, 1)),
            (self.client, "oct-sixth", date(2026, 10, 6)),
            (self.client, "oct-fourteenth", date(2026, 10, 14)),
            (self.client, "next-month", date(2026, 11, 1)),
            (other, "foreign-client", date(2026, 10, 2)),
        ):
            source = EDI835File.objects.create(
                client=client, original_filename=name + ".835",
                stored_filename=name + ".835",
                claims_count=1, held_claims_count=1,
                conversion_findings=[{
                    "severity": "HOLD", "claim_index": "1",
                    "reason": "Waiting for review",
                }],
            )
            EDI835File.objects.filter(pk=source.pk).update(
                uploaded_at=datetime.combine(day, time(12), tzinfo=EASTERN)
            )
            EDI835Claim.objects.create(
                edi_file=source, claim_sequence=1, highmark_claim_number=name,
                total_charge_amount=Decimal("100.00"),
            )

    def check(self, **params):
        request = self.factory.get("/edi835/api/client-portal/attention/", params)
        request.user = self.viewer
        with patch("edi835.client_portal_period.timezone.now", return_value=FROZEN_TIME):
            response = attention_claims(request)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_monthly_contains_only_october_and_orders_oldest_first(self):
        result = self.check(period="monthly")
        self.assertEqual(result["period"]["from"], "2026-10-01")
        self.assertEqual(result["period"]["to"], "2026-10-31")
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            [r["number"] for r in result["claims"]],
            ["oct-first", "oct-sixth", "oct-fourteenth"],
        )
        self.assertEqual([r["days"] for r in result["claims"]], [14, 9, 1])

    def test_monthly_pagination_keeps_same_range_and_sort(self):
        first = self.check(period="monthly", page=1, page_size=2)
        second = self.check(period="monthly", page=2, page_size=2)
        self.assertEqual(first["pages"], 2)
        self.assertEqual([r["number"] for r in first["claims"]], ["oct-first", "oct-sixth"])
        self.assertEqual([r["number"] for r in second["claims"]], ["oct-fourteenth"])

    def test_weekly_contains_current_sunday_saturday_only(self):
        result = self.check(period="weekly")
        self.assertEqual(result["period"]["from"], "2026-10-11")
        self.assertEqual(result["period"]["to"], "2026-10-17")
        self.assertEqual([r["number"] for r in result["claims"]], ["oct-fourteenth"])

    def test_custom_contains_inclusive_date_range_and_orders_by_age(self):
        result = self.check(period="custom", **{
            "from": "2026-10-06", "to": "2026-10-14",
        })
        self.assertEqual([r["number"] for r in result["claims"]],
                         ["oct-sixth", "oct-fourteenth"])

    def test_all_includes_all_own_records_but_no_foreign_records(self):
        result = self.check(period="all")
        self.assertEqual(result["count"], 5)
        self.assertNotIn("foreign-client", [r["number"] for r in result["claims"]])

    def test_daily_shows_no_unrelated_historical_holds(self):
        result = self.check(period="daily")
        self.assertEqual(result["count"], 0)
