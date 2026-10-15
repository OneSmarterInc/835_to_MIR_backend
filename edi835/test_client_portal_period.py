"""Date-range semantics for tenant-only FileX Client User portal views."""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import Client, User
from edi835.client_portal_period import InvalidPeriod, apply_period, resolve_period
from edi835.client_portal_views import overview, history
from edi835.client_portal_holds import attention_claims
from edi835.models import EDI835File, EDI835Claim, MIRFile


ZONE = ZoneInfo("America/New_York")


def at(day, hour=12):
    return datetime.combine(day, time(hour=hour), tzinfo=ZONE)


class PeriodResolverTests(TestCase):
    def setUp(self):
        self.client = Client.objects.create(
            name="Period Test", client_code="PERIOD-TEST", email="period@test.example",
            timezone="America/New_York",
        )

    def test_week_starts_sunday_ends_saturday(self):
        p = resolve_period({"period": "weekly"}, self.client, today=date(2026, 10, 15))
        self.assertEqual((p["from"], p["to"]), (date(2026, 10, 11), date(2026, 10, 17)))
        sunday = resolve_period({"period": "weekly"}, self.client, today=date(2026, 10, 11))
        self.assertEqual((sunday["from"], sunday["to"]), (date(2026, 10, 11), date(2026, 10, 17)))

    def test_month_is_complete_calendar_month(self):
        p = resolve_period({"period": "monthly"}, self.client, today=date(2026, 2, 17))
        self.assertEqual((p["from"], p["to"]), (date(2026, 2, 1), date(2026, 2, 28)))
        leap = resolve_period({"period": "monthly"}, self.client, today=date(2028, 2, 15))
        self.assertEqual(leap["to"], date(2028, 2, 29))

    def test_custom_is_inclusive_and_validated(self):
        p = resolve_period({"period": "custom", "from": "2026-09-02", "to": "2026-09-04"},
                           self.client, today=date(2026, 10, 15))
        self.assertEqual(p["start"], at(date(2026, 9, 2), 0))
        self.assertEqual(p["end"], at(date(2026, 9, 5), 0))
        for bad in (
            {"period": "custom", "from": "2026-09-05", "to": "2026-09-04"},
            {"period": "custom", "from": "2026-09-02", "to": ""},
            {"period": "missing"},
        ):
            with self.assertRaises(InvalidPeriod):
                resolve_period(bad, self.client, today=date(2026, 10, 15))

    def test_all_has_no_boundaries(self):
        p = resolve_period({"period": "all"}, self.client, today=date(2026, 10, 15))
        self.assertIsNone(p["from"])
        self.assertIsNone(p["to"])
        self.assertIsNone(p["start"])
        self.assertIsNone(p["end"])


class PortalPeriodQueryTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(
            name="Alpha Period", client_code="ALPHA-PERIOD", email="alpha@period.example",
            timezone="America/New_York",
        )
        self.other = Client.objects.create(
            name="Beta Period", client_code="BETA-PERIOD", email="beta@period.example",
            timezone="America/New_York",
        )
        self.user = User.objects.create_user(
            email="viewer@period.example", name="Client Viewer", mobile="+12125550409",
            password="DummyVeryLongPassword!", client=self.client, client_role="CLIENT_USER",
        )
        self.inside = self.file(
            self.client, "inside.835", date(2026, 9, 3), billed="125.00",
            paid="95.00", patient="30.00", held=1,
        )
        self.outside = self.file(
            self.client, "outside.835", date(2026, 9, 8), billed="800.00",
            paid="300.00", patient="500.00", held=3,
        )
        self.foreign = self.file(
            self.other, "foreign.835", date(2026, 9, 3), billed="9000.00",
            paid="8000.00", patient="1000.00", held=6,
        )
        for f, day in ((self.inside, date(2026, 9, 3)),
                       (self.outside, date(2026, 9, 8)),
                       (self.foreign, date(2026, 9, 3))):
            mir = MIRFile.objects.create(
                source_835=f, client=f.client, mir_filename=f.original_filename + ".mir",
                file_content="test", file_hash=f.original_filename,
                claim_count=1, status="PUSHED", converted_at=at(day),
            )
            MIRFile.objects.filter(pk=mir.pk).update(updated_at=at(day))

    def file(self, client, filename, day, *, billed, paid, patient, held):
        source = EDI835File.objects.create(
            client=client, original_filename=filename, stored_filename=filename,
            claims_count=1, held_claims_count=held,
            conversion_findings=[
                {"severity": "HOLD", "claim_index": "1", "reason": "Review payer response"}
            ],
        )
        EDI835File.objects.filter(pk=source.pk).update(uploaded_at=at(day))
        EDI835Claim.objects.create(
            edi_file=source, claim_sequence=1,
            highmark_claim_number=filename + "-001",
            total_charge_amount=Decimal(billed), paid_amount=Decimal(paid),
            patient_responsibility=Decimal(patient),
        )
        return source

    def get(self, view, suffix, params):
        request = self.factory.get(suffix, data=params)
        request.user = self.user
        response = view(request)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_custom_financial_counts_and_preview_are_same_period(self):
        window = {"period": "custom", "from": "2026-09-02", "to": "2026-09-04"}
        result = self.get(overview, "/edi835/api/client-portal/overview/", window)
        self.assertEqual(result["metrics"]["received_files"], 1)
        self.assertEqual(result["metrics"]["waiting_claims"], 1)
        self.assertEqual(result["metrics"]["received_claims"], 1)
        self.assertEqual(result["metrics"]["billed_amount"], "125")
        self.assertEqual(result["metrics"]["paid_amount"], "95")
        self.assertEqual(result["metrics"]["patient_amount"], "30")
        self.assertEqual(result["metrics"]["sent_files"], 1)
        self.assertEqual({item["filename"] for item in result["issues"]}, {"inside.835"})
        self.assertEqual(len(result["claims"]), 1)
        attention = self.get(attention_claims, "/edi835/api/client-portal/attention/", window)
        self.assertEqual(attention["count"], 1)
        self.assertEqual(attention["claims"][0]["file_name"], "inside.835")
        file_history = self.get(history, "/edi835/api/client-portal/history/", window)
        self.assertEqual(file_history["count"], 1)

    def test_all_is_unbounded_but_never_crosses_tenants(self):
        result = self.get(overview, "/edi835/api/client-portal/overview/", {"period": "all"})
        self.assertEqual(result["metrics"]["received_files"], 2)
        self.assertEqual(result["metrics"]["waiting_claims"], 4)
        self.assertEqual(result["metrics"]["normalized_claims"], 2)
        self.assertEqual(Decimal(result["metrics"]["billed_amount"]), Decimal("925.00"))
        attention = self.get(attention_claims, "/edi835/api/client-portal/attention/", {"period": "all"})
        self.assertEqual(attention["count"], 2)
        self.assertEqual(history_page := self.get(history, "/edi835/api/client-portal/history/", {"period": "all"})["count"], 2)

    def test_daily_does_not_include_older_holds(self):
        result = self.get(overview, "/edi835/api/client-portal/overview/", {"period": "daily"})
        self.assertEqual(result["period"]["from"], timezone.localtime(timezone.now(), ZONE).date().isoformat())
        self.assertEqual(result["metrics"]["waiting_claims"], 0)

    def test_custom_invalid_dates_rejected_by_each_api(self):
        params = {"period": "custom", "from": "2026-09-09", "to": "2026-09-02"}
        for view, url in (
            (overview, "/edi835/api/client-portal/overview/"),
            (attention_claims, "/edi835/api/client-portal/attention/"),
            (history, "/edi835/api/client-portal/history/"),
        ):
            request = self.factory.get(url, params)
            request.user = self.user
            self.assertEqual(view(request).status_code, 400)
