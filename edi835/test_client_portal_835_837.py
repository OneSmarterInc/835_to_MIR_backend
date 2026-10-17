import csv
import io
import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from .models import EDI835File, EDI835Claim, EDI837File, EDI837Claim
from .client_portal_835_837 import comparison_835_837


class Client835837Tests(TestCase):
    def setUp(self):
        self.client_a = Client.objects.create(name="A", client_code="C837A", email="a@example.com", timezone="America/Chicago")
        self.client_b = Client.objects.create(name="B", client_code="C837B", email="b@example.com")
        self.user = User.objects.create_user(email="status@example.com", name="Viewer", mobile="15555550134",
                                             client=self.client_a, client_role="CLIENT_USER", password="TestPassword!234")
        self.factory = RequestFactory()

    def add(self, kind, number, billed="100", internal="I12345", day=1, client=None, hour=12):
        client = client or self.client_a
        model = EDI837File if kind == "837" else EDI835File
        file = model.objects.create(client=client, original_filename=number + "." + kind, stored_filename=number + "." + kind)
        model.objects.filter(pk=file.pk).update(uploaded_at=datetime(2026, 10, day, hour, tzinfo=timezone.utc))
        fields = dict(edi_file=file, claim_sequence=1, highmark_claim_number=number,
                      internal_claim_number=internal, total_charge_amount=billed)
        if kind == "837":
            fields.update(client=client, claim_control_number=number)
        return (EDI837Claim if kind == "837" else EDI835Claim).objects.create(**fields)

    def request(self, **query):
        request = self.factory.get("/comparison-835-837/", {"period": "all", **query})
        request.user = self.user
        with patch("edi835.client_portal_period.timezone.now", return_value=datetime(2026, 10, 16, 18, tzinfo=timezone.utc)):
            return comparison_835_837(request)

    def data(self, **query):
        response = self.request(**query)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_only_billed_mismatches_in_both(self):
        self.add("837", "111", "100")
        paid = self.add("835", "111", "90")
        paid.paid_amount = 100
        paid.save()
        self.add("837", "222", "100")
        self.add("835", "222", "100")
        self.add("837", "333", "0")
        self.add("835", "333", "0")
        data = self.data()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["difference"], "10.00")
        self.assertEqual(data["rows"][0]["waiting_days"], 15)
        self.assertEqual(data["age_summary"]["counts"]["10-20"], 1)

    def test_837_only_keeps_missing_amounts_unknown_and_repeated_arrivals(self):
        self.add("837", "111", "0")
        self.add("837", "111", "0", day=2)
        self.add("835", "111", "0", client=self.client_b)
        data = self.data(category="837-only")
        self.assertEqual(data["count"], 2)
        self.assertEqual(len({r["key"] for r in data["rows"]}), 2)
        for row in data["rows"]:
            self.assertIsNone(row["billed_835"])
            self.assertIsNone(row["difference"])

    def test_remittance_outside_selected_period_still_counts_as_present(self):
        self.add("837", "111", day=1)
        self.add("835", "111", "99", day=16)
        filters = {"period": "custom", "from": "2026-10-01", "to": "2026-10-01"}
        self.assertEqual(self.data(**filters)["count"], 1)
        self.assertEqual(self.data(category="837-only", **filters)["count"], 0)

    def test_internal_identity_and_missing_internal_fallback(self):
        self.add("837", "111", internal="A12345")
        self.add("835", "111", "90", internal="B12345")
        self.add("837", "222", internal="")
        self.add("835", "222", "90", internal="ZZ:C12345")
        self.add("837", "333", internal="D12345")
        self.add("835", "333", "90", internal="ZZ:D12345")
        self.assertEqual({r["claim"] for r in self.data()["rows"]}, {"222", "333"})
        self.assertEqual(self.data(category="837-only")["rows"][0]["claim"], "111")

    def test_duplicates_and_ambiguous_missing_internal_are_explained(self):
        self.add("837", "111")
        self.add("835", "111", "80")
        self.add("835", "111", "90")
        self.add("837", "222", internal="A12345")
        self.add("837", "222", internal="B12345")
        self.add("835", "222", internal="")
        result = self.data()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["ambiguous_claims"], 3)
        self.assertEqual(self.data(category="837-only")["count"], 0)

    def test_filters_pagination_sort_and_csv_use_same_rows(self):
        for n, day in enumerate((1, 2, 15)):
            self.add("837", str(n), str(100 + n), day=day)
            self.add("835", str(n), "90")
        filters = dict(age_band="10-20", page_size=1, sort="difference", direction="desc")
        result = self.data(**filters)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["age_summary"]["counts"]["10-20"], 2)
        self.assertEqual(result["rows"][0]["claim"], "1")
        self.assertEqual(self.data(page=2, **filters)["rows"][0]["claim"], "0")
        response = self.request(format="csv", **filters)
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual([r["Claim"] for r in rows], ["1", "0"])
        self.assertEqual(len(rows[0]), 8)
        self.assertIn("no-store", response["Cache-Control"])

    def test_tenant_isolation_and_client_role(self):
        self.add("837", "111", client=self.client_b)
        self.assertEqual(self.data(category="837-only")["count"], 0)
        self.user.client_role = "CLIENT_ADMIN"
        self.assertEqual(self.request().status_code, 403)

    def test_period_uses_client_timezone(self):
        self.add("837", "111", day=2, hour=2)  # October 1 in Chicago.
        result = self.data(category="837-only", period="custom", **{"from": "2026-10-01", "to": "2026-10-01"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["waiting_days"], 15)

    def test_invalid_filters_rejected(self):
        for params in ({"category": "all"}, {"age_band": "negative"}, {"sort": "paid_mir"},
                       {"direction": "bad"}, {"format": "html"}, {"page": "bad"}):
            self.assertEqual(self.request(**params).status_code, 400)

    def test_csv_escapes_source_filename(self):
        row = self.add("837", "111")
        EDI837File.objects.filter(pk=row.edi_file_id).update(original_filename="=DANGEROUS.837")
        response = self.request(category="837-only", format="csv")
        exported = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual(exported[0]["837 source"], "'=DANGEROUS.837")
