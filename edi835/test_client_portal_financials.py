"""Financial source, range, isolation and query-count regression coverage."""
import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from .client_portal_financials import financial_metrics
from .client_portal_period import resolve_period
from .client_portal_views import overview
from .edi835_claim_service import normalize_835_file
from .mir_persistence import store_mir_file
from .models import (
    EDI835File, EDI835Claim, MIRFile, MIRClaim, MIRClaimChunk,
    MIRServiceLine, RECONFile, RECONClaim, RECONServiceLine,
)
from .recon_service import process_recon_file


ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 15, 12, tzinfo=ZONE)


class ClientFinancialTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(
            name="Financial Alpha", client_code="FIN-A", email="a@finance.example",
            timezone=str(ZONE),
        )
        self.other = Client.objects.create(
            name="Financial Beta", client_code="FIN-B", email="b@finance.example",
        )
        self.user = User.objects.create_user(
            email="viewer@finance.example", name="Viewer", mobile="+12125550701",
            password="TestOnlyPassword!", client=self.tenant, client_role="CLIENT_USER",
        )

    def source(self, at=NOW, amount="100.00", tenant=None, **kwargs):
        source = EDI835File.objects.create(
            client=tenant or self.tenant, original_filename="source.835",
            stored_filename="source.835", claims_count=1, **kwargs,
        )
        EDI835File.objects.filter(pk=source.pk).update(uploaded_at=at)
        EDI835Claim.objects.create(
            edi_file=source, claim_sequence=1, highmark_claim_number="SAME-CLAIM",
            total_charge_amount=Decimal(amount), paid_amount=Decimal("12.34"),
            patient_responsibility=Decimal("4.56"), service_count=5,
        )
        return source

    def mir(self, source, at=NOW, amounts=("40.00", "40.00"), **kwargs):
        output = MIRFile.objects.create(
            client=source.client, source_835=source, converted_at=at,
            mir_filename="output.MIR", claim_count=1, **kwargs,
        )
        claim = MIRClaim.objects.create(
            mir_file=output, claim_sequence=1, claim_control_number="SAME-CLAIM",
            chunk_count=len(amounts), header_raw=" " * 334,
        )
        for index, amount in enumerate(amounts, 1):
            chunk = MIRClaimChunk.objects.create(
                mir_claim=claim, chunk_number=index, physical_row_number=index,
                services_in_chunk=1, row_length=637, raw_row=" " * 637,
            )
            MIRServiceLine.objects.create(
                mir_claim=claim, mir_chunk=chunk, service_sequence=index,
                chunk_service_sequence=1, charge_amount=Decimal(amount),
                paid_amount=Decimal("7.00"), allowed_amount=Decimal("9.00"),
            )
        return output

    def recon(self, at=NOW, amount="25.00", tenant=None, **kwargs):
        tenant = tenant or self.tenant
        kwargs.setdefault("status", "PROCESSED")
        file = RECONFile.objects.create(
            client=tenant, original_filename="recon.csv", stored_filename="recon.csv",
            # Deliberately different rollups: dashboard must read claim paid amounts.
            total_charge_amount=Decimal("9000"), total_paid_amount=Decimal("8000"),
            processed_at=at + timedelta(days=2), **kwargs,
        )
        RECONFile.objects.filter(pk=file.pk).update(uploaded_at=at)
        claim = RECONClaim.objects.create(
            recon_file=file, client=tenant, claim_sequence=1,
            claim_control_number="SAME-CLAIM", paid_amount=Decimal(amount),
            charge_amount=Decimal("500"), allowed_amount=Decimal("400"),
            mir907_amount=Decimal("300"), patient_responsibility=Decimal("200"),
        )
        # Service rows must not multiply or supplement claim-level rollups.
        for index in (1, 2):
            RECONServiceLine.objects.create(
                recon_file=file, recon_claim=claim, service_sequence=index,
                source_row_number=index, paid_amount=Decimal("90.00"),
            )
        return file

    def totals(self, query=None, today=date(2026, 10, 15)):
        return financial_metrics(self.tenant, resolve_period(
            query or {"period": "all"}, self.tenant, today=today,
        ))

    def assert_amounts(self, metrics, expected):
        self.assertEqual(
            tuple(metrics[key] for key in ("billed_835_amount", "billed_mir_amount", "recon_amount")),
            expected,
        )

    def test_independent_grains_equal_values_continuations_and_repeated_claim_ids(self):
        self.mir(self.source())
        self.mir(self.source(), amounts=("-10.25", "0.25"))
        self.recon()
        self.recon(amount="-5.00")
        self.recon(amount="0.10")
        result = self.totals()
        self.assert_amounts(result, ("200.00", "70.00", "20.10"))
        self.assertEqual(result["billed_835_claims"], 2)
        self.assertEqual(result["billed_mir_services"], 4)
        self.assertEqual(result["recon_claims"], 3)

    def test_streams_use_independent_event_dates(self):
        old = NOW - timedelta(days=10)
        self.mir(self.source(at=old), at=NOW)
        self.mir(self.source(at=NOW, amount="200.00"), at=old)
        self.recon(at=NOW)
        self.recon(at=old, amount="900.00")
        self.assert_amounts(self.totals({"period": "daily"}), ("200.00", "80.00", "25.00"))

    def test_synthetic_release_is_not_another_received_835_but_its_mir_is_real(self):
        self.source()
        self.mir(self.source(ingestion_source="HELD_RELEASE"))
        self.assert_amounts(self.totals(), ("100.00", "80.00", "0.00"))

    def test_recon_excludes_other_file_kinds_and_unusable_processing_states(self):
        for status in ("UPLOADED", "PROCESSING", "FAILED"):
            self.recon(status=status, amount="1000.00")
        self.recon(file_kind="837", amount="1000.00")
        self.recon(status="PARTIAL", amount="2.00")
        self.recon(amount="3.00")
        self.assert_amounts(self.totals(), ("0.00", "0.00", "5.00"))

    def test_tenant_isolation_including_inconsistent_parent_ownership(self):
        self.mir(self.source())
        self.recon()
        foreign_mir = self.mir(self.source(tenant=self.other))
        foreign_recon = self.recon(tenant=self.other)
        self.assert_amounts(self.totals(), ("100.00", "80.00", "25.00"))
        MIRFile.objects.filter(pk=foreign_mir.pk).update(client=self.tenant)
        RECONClaim.objects.filter(recon_file=foreign_recon).update(client=self.tenant)
        own_file = self.recon(amount="1000.00")
        RECONClaim.objects.filter(recon_file=own_file).update(client=self.other)
        self.assert_amounts(self.totals(), ("100.00", "80.00", "25.00"))

    def test_three_queries_regardless_of_number_of_files(self):
        period = resolve_period({"period": "all"}, self.tenant)
        with self.assertNumQueries(3):
            financial_metrics(self.tenant, period)
        for _ in range(6):
            self.mir(self.source())
            self.recon()
        with self.assertNumQueries(3):
            self.assert_amounts(financial_metrics(self.tenant, period), ("600.00", "480.00", "150.00"))

    def test_all_includes_distant_history_and_future(self):
        for at in (NOW - timedelta(days=400), NOW + timedelta(days=400)):
            self.mir(self.source(at=at), at=at)
            self.recon(at=at)
        self.assert_amounts(self.totals(), ("200.00", "160.00", "50.00"))

    def test_empty_is_distinguishable_from_recorded_zero(self):
        empty = self.totals()
        self.assert_amounts(empty, ("0.00", "0.00", "0.00"))
        self.assertEqual([empty[k] for k in ("billed_835_claims", "billed_mir_services", "recon_claims")], [0, 0, 0])
        self.mir(self.source(amount="0.00"), amounts=("0.00",))
        self.recon(amount="0.00")
        zero = self.totals()
        self.assert_amounts(zero, ("0.00", "0.00", "0.00"))
        self.assertEqual([zero[k] for k in ("billed_835_claims", "billed_mir_services", "recon_claims")], [1, 1, 1])

    def test_each_period_includes_first_and_last_instants_only(self):
        cases = (
            ({"period": "daily"}, date(2026, 10, 15), "2026-10-15", "2026-10-16"),
            ({"period": "weekly"}, date(2026, 10, 15), "2026-10-11", "2026-10-18"),
            ({"period": "monthly"}, date(2026, 10, 15), "2026-10-01", "2026-11-01"),
            ({"period": "monthly"}, date(2028, 2, 15), "2028-02-01", "2028-03-01"),
            ({"period": "custom", "from": "2026-09-02", "to": "2026-09-04"}, date(2026, 10, 15), "2026-09-02", "2026-09-05"),
            ({"period": "custom", "from": "2026-03-08", "to": "2026-03-08"}, date(2026, 3, 8), "2026-03-08", "2026-03-09"),
            ({"period": "custom", "from": "2026-11-01", "to": "2026-11-01"}, date(2026, 11, 1), "2026-11-01", "2026-11-02"),
        )
        for query, today, start, end in cases:
            with self.subTest(query=query, today=today):
                EDI835File.objects.all().delete()
                RECONFile.objects.all().delete()
                first = datetime.fromisoformat(start).replace(tzinfo=ZONE).astimezone(dt_timezone.utc)
                after = datetime.fromisoformat(end).replace(tzinfo=ZONE).astimezone(dt_timezone.utc)
                for at in (first - timedelta(microseconds=1), first, after - timedelta(microseconds=1), after):
                    self.mir(self.source(at=at), at=at)
                    self.recon(at=at)
                self.assert_amounts(self.totals(query, today), ("200.00", "160.00", "50.00"))

    def test_overview_defaults_to_today_in_configured_timezone_and_ignores_tenant_parameter(self):
        instant = datetime(2026, 10, 16, 1, tzinfo=dt_timezone.utc)
        self.mir(self.source(at=instant), at=instant)
        self.recon(at=instant)
        request = RequestFactory().get("/edi835/api/client-portal/overview/", {"client_id": str(self.other.pk)})
        request.user = self.user
        with patch("edi835.client_portal_period.timezone.now", return_value=instant):
            response = overview(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["period"]["from"], "2026-10-15")
        self.assertEqual(data["period"]["timezone"], str(ZONE))
        self.assert_amounts(data["metrics"], ("100.00", "80.00", "25.00"))
        self.assertEqual(data["metrics"]["billed_amount"], "100.00")
        self.assertEqual(data["metrics"]["paid_amount"], "12.34")

    def test_client_timezone_selection_changes_local_day_without_changing_events(self):
        instant = datetime(2026, 10, 15, 2, tzinfo=dt_timezone.utc)
        self.mir(self.source(at=instant), at=instant)
        self.recon(at=instant)
        self.assert_amounts(self.totals({"period": "daily"}), ("0.00", "0.00", "0.00"))
        self.tenant.timezone = "Asia/Kolkata"
        self.assert_amounts(self.totals({"period": "daily"}), ("100.00", "80.00", "25.00"))

    def test_unrepresentable_custom_end_is_a_validation_error(self):
        request = RequestFactory().get("/", {"period": "custom", "from": "9999-12-31", "to": "9999-12-31"})
        request.user = self.user
        self.assertEqual(overview(request).status_code, 400)

    def test_actual_normalizers_and_reprocessing_do_not_duplicate_totals(self):
        source = EDI835File.objects.create(
            client=self.tenant, original_filename="parsed.835", stored_filename="parsed.835",
            input_file_content="CLP*SAME-CLAIM*1*125.50*50.25*12.00~SVC*HC:99213*125.50*50.25~",
        )
        normalize_835_file(source)
        normalize_835_file(source)
        header = list(" " * 334)
        header[2:25] = "12345678901234567ABC123".ljust(23)
        header[248:252] = "0101"
        header[332:334] = "01"
        service = list(" " * 303)
        service[50:61] = "00000008750"
        service[94:105] = "00000003325"
        mir_text = "".join(header + service)
        store_mir_file(source_835=source, mir_filename="parsed.MIR", mir_text=mir_text)
        store_mir_file(source_835=source, mir_filename="parsed.MIR", mir_text=mir_text)
        recon = RECONFile.objects.create(
            client=self.tenant, original_filename="parsed.csv", stored_filename="parsed.csv",
            file_content="claim_control_number,charge_amount,amount_in_recon\nSAME-CLAIM,99.00,20.10\nSAME-CLAIM,88.00,1.15\n",
        )
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            process_recon_file(recon)
            process_recon_file(recon)
        recon.refresh_from_db()
        self.assertEqual(recon.status, "PROCESSED")
        self.assertEqual(recon.total_paid_amount, Decimal("21.25"))
        self.assert_amounts(self.totals(), ("125.50", "87.50", "21.25"))
