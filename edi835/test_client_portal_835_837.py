import csv
import io
import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
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

    def test_exact_signed_matches_excluded_in_sql(self):
        self.add("837", "111", "100")
        paid = self.add("835", "111", "90")
        paid.paid_amount = 100
        paid.save()
        self.add("837", "222", "100")
        self.add("835", "222", "100")
        self.add("837", "333", "0")
        self.add("835", "333", "0")
        self.add("837", "444", "-100")
        self.add("835", "444", "-100")
        self.add("837", "555", "100")
        self.add("835", "555", "-100")
        self.add("837", "666", "-100")
        self.add("835", "666", "100")
        with CaptureQueriesContext(connection) as queries:
            data = self.data()
        self.assertEqual({r["claim"]: r["difference"] for r in data["rows"]},
                         {"111": "10.00", "555": "200.00", "666": "200.00"})
        amount_queries = [q["sql"] for q in queries if 'AS "_billed_835"' in q["sql"]]
        self.assertTrue(amount_queries, "Amounts must be filtered by the database")
        self.assertTrue(all('NOT (' in q and 'total_charge_amount' in q for q in amount_queries))
        self.assertTrue(all(r["status"] == "Billed amounts differ" for r in data["rows"]))
        self.assertEqual(data["rows"][0]["waiting_days"], 15)
        self.assertEqual(data["age_summary"]["counts"]["10-20"], 3)
        self.assertEqual(data["amount_summary"], {"matched": 0, "mismatched": 3, "missing_835": 0})
        self.assertEqual(self.data(category="837-only")["count"], 0)

    def test_pagination_and_csv_exclude_exact_matches(self):
        self.add("837", "111", "100", day=1)
        self.add("835", "111", "100")
        self.add("837", "222", "100", day=15)
        self.add("835", "222", "99.99")
        self.assertEqual(self.data(page_size=1)["rows"][0]["claim"], "222")
        self.assertEqual(self.data(page_size=1, page=2)["count"], 1)
        self.assertEqual(self.data(page_size=1, page=2)["pages"], 1)
        response = self.request(format="csv", page_size=1)
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual([r["Claim"] for r in rows], ["222", "222"])
        self.assertEqual(rows[0]["Billed difference"], "0.01")
        self.assertEqual(self.data(sort="waiting_days", direction="desc")["rows"][0]["claim"], "222")
        self.assertEqual(self.data(age_band="10-20")["amount_summary"]["mismatched"], 0)

    def test_search_filters_counts_chart_and_csv(self):
        for number, day in (("ABC111", 1), ("ABC222", 15), ("OTHER", 1)):
            self.add("837", number, "100", day=day)
            self.add("835", number, "99")
        self.add("837", "ABC333", "100")
        self.add("835", "ABC333", "100")
        self.add("837", "ABC444", "100", client=self.client_b)
        self.add("835", "ABC444", "99", client=self.client_b)
        result = self.data(search=" abc ", page_size=1)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["age_summary"]["total"], 2)
        self.assertEqual(self.data(search="ABC", age_band="10-20")["count"], 1)
        self.assertEqual(self.data(search="i12345")["count"], 3)
        self.assertEqual(self.data(search="abc111.837")["count"], 1)
        self.assertEqual(self.data(search="%_")["count"], 0)
        response = self.request(search="abc", format="csv", page_size=1)
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual({r["Claim"] for r in rows}, {"ABC111", "ABC222"})

    def test_search_cannot_hide_duplicate_occurrences(self):
        first = self.add("837", "111")
        EDI837File.objects.filter(pk=first.edi_file_id).update(original_filename="find-me.837")
        self.add("837", "111")
        self.add("835", "111", "90")
        self.assertEqual(self.data(search="find-me")["count"], 1)
        self.assertEqual(self.data(search="find-me")["occurrence_count"], 3)
        self.assertEqual(self.data(search="find-me")["ambiguous_claims"], 1)
        self.add("837", "MISSING", "100")
        self.assertEqual(self.data(category="837-only", search="missing")["count"], 1)
        self.assertEqual(self.data(category="837-only", search="no-such-file")["count"], 0)

    def test_sql_pairing_across_batch_boundary(self):
        for index in range(101):
            number = str(1000 + index)
            self.add("837", number, "100")
            self.add("835", number, "90" if index == 100 else "100", internal="DIFFERENT")
        with CaptureQueriesContext(connection) as queries:
            result = self.data()
        self.assertEqual([r["claim"] for r in result["rows"]], ["1100"])
        self.assertLessEqual(len(queries), 7, "Batch pairing must not issue a query per claim")

    def test_837_only_keeps_missing_amounts_unknown_and_repeated_arrivals(self):
        self.add("837", "111", "0")
        self.add("837", "111", "0", day=2)
        self.add("835", "111", "0", client=self.client_b)
        data = self.data(category="837-only")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["occurrence_count"], 2)
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

    def test_unique_claim_number_matches_despite_different_internal_references(self):
        self.add("837", "111", internal="A12345")
        self.add("835", "111", "90", internal="B12345")
        self.add("837", "222", internal="")
        self.add("835", "222", "90", internal="ZZ:C12345")
        self.add("837", "333", internal="D12345")
        self.add("835", "333", "90", internal="ZZ:D12345")
        self.assertEqual({r["claim"] for r in self.data()["rows"]}, {"111", "222", "333"})
        self.assertEqual(self.data(category="837-only")["count"], 0)

    def test_parser_identifiers_can_differ_for_the_same_claim(self):
        from .edi837_service import _837_claim_numbers
        from .edi835_claim_service import normalized_835_claims
        submitted = _837_claim_numbers("123456789QYN071", "H100001346867370")
        received = normalized_835_claims("CLP*123456789*1*90*40*0*ZZ*QYN071~")[0]
        self.assertNotEqual(submitted["internal_claim_number"], received["internal_claim_number"])
        self.add("837", submitted["highmark_claim_number"], "100", internal=submitted["internal_claim_number"])
        self.add("835", received["highmark_claim_number"], "90", internal=received["internal_claim_number"])
        result = self.data()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["difference"], "10.00")
        self.assertEqual(self.data(category="837-only")["count"], 0)

    def test_conflicting_occurrences_are_ambiguous_not_missing(self):
        self.add("837", "111", internal="A12345")
        self.add("837", "111", internal="B12345")
        self.add("835", "111", "90", internal="C12345")
        self.assertEqual(self.data()["ambiguous_claims"], 1)
        self.assertEqual(self.data()["count"], 1)
        self.assertEqual(self.data(category="837-only")["count"], 0)

    def test_duplicates_and_ambiguous_missing_internal_are_explained(self):
        self.add("837", "111")
        self.add("835", "111", "80")
        self.add("835", "111", "90")
        self.add("837", "222", internal="A12345")
        self.add("837", "222", internal="B12345")
        self.add("835", "222", internal="")
        result = self.data()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["ambiguous_claims"], 2)
        self.assertEqual(result["occurrence_count"], 6)
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
        self.assertEqual([r["Claim"] for r in rows], ["1", "1", "0", "0"])
        self.assertEqual(len(rows[0]), 12)
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
                       {"direction": "bad"}, {"format": "html"}, {"page": "bad"}, {"search": "x" * 101}):
            self.assertEqual(self.request(**params).status_code, 400)

    def test_csv_escapes_source_filename(self):
        row = self.add("837", "111")
        EDI837File.objects.filter(pk=row.edi_file_id).update(original_filename="=DANGEROUS.837")
        response = self.request(category="837-only", format="csv")
        exported = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual(exported[0]["Source file"], "'=DANGEROUS.837")

    def test_occurrences_keep_files_dates_amounts_and_do_not_cross_join(self):
        first = self.add("837", "MULTI", "100", day=1)
        second = self.add("837", "MULTI", "200", day=15)
        third = self.add("835", "MULTI", "100", day=16)
        fourth = self.add("835", "MULTI", "150", day=16)
        self.add("837", "OTHER", "100")
        self.add("835", "OTHER", "90")
        result = self.data(search="multi.835", page_size=1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["occurrence_count"], 4)
        self.assertEqual(result["age_summary"]["counts"]["10-20"], 1)
        row = result["rows"][0]
        self.assertTrue(row["ambiguous"])
        self.assertIsNone(row["difference"])
        files = row["occurrences"]
        self.assertEqual({item["file_id"] for item in files}, {str(c.edi_file_id) for c in (first, second, third, fourth)})
        self.assertEqual({item["billed_837"] for item in files}, {"100.00", "200.00", None})
        self.assertEqual({item["billed_835"] for item in files}, {"100.00", "150.00", None})
        self.assertEqual(files[0]["received_at"], "2026-10-01T07:00:00-05:00")
        self.assertEqual(self.data(search="multi", age_band="0-10")["count"], 0)
        self.assertEqual(self.data(page_size=1, sort="claim", direction="asc")["occurrence_count"], 6)
        self.assertEqual(len(self.data(page_size=1, sort="claim", direction="asc")["rows"][0]["occurrences"]), 4)
        self.assertEqual(self.data(page_size=1, page=2, sort="claim", direction="asc")["rows"][0]["claim"], "OTHER")

    def test_multiple_claims_in_one_file_are_not_deduplicated(self):
        first = self.add("837", "MULTI", "100")
        EDI837Claim.objects.create(edi_file=first.edi_file, client=self.client_a, claim_sequence=2,
            highmark_claim_number="MULTI", internal_claim_number=first.internal_claim_number,
            claim_control_number="MULTI", total_charge_amount="100")
        self.add("835", "MULTI", "100")
        data = self.data()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["occurrence_count"], 3)
        self.assertIsNone(data["rows"][0]["difference"])
        response = self.request(format="csv")
        rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["Claim occurrence"] for r in rows[:2]], ["1", "2"])
        self.assertEqual(len({r["File ID"] for r in rows}), 2)
