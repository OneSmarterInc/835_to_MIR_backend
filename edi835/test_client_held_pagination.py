"""Verify held claims pages and tenant separation for Needs Attention."""
import json
from decimal import Decimal

from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from edi835.models import EDI835File, EDI835Claim
from edi835.client_portal_holds import held_file_claims


class HeldFilePaginationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(name="Alpha", client_code="ALPHA-HP", email="a@example.org")
        self.other = Client.objects.create(name="Beta", client_code="BETA-HP", email="b@example.org")
        self.user = User.objects.create_user(
            email="held-review@example.org", name="Viewer", mobile="+14155550912",
            password="LongDummyPassword!", client=self.client, client_role="CLIENT_USER",
        )
        findings = [
            {"severity": "HOLD", "claim_index": str(n), "reason": "Missing plan ID"}
            for n in range(1, 46)
        ]
        self.file = EDI835File.objects.create(
            client=self.client, original_filename="held.835",
            stored_filename="held.835", claims_count=45, held_claims_count=45,
            conversion_findings=findings,
        )
        EDI835Claim.objects.bulk_create([
            EDI835Claim(
                edi_file=self.file, claim_sequence=n, highmark_claim_number=f"TEST{n:05d}",
                total_charge_amount=Decimal("100.00"),
            )
            for n in range(1, 46)
        ])
        self.foreign = EDI835File.objects.create(
            client=self.other, original_filename="private.835", stored_filename="private.835",
            held_claims_count=3,
        )

    def test_next_page_changes_rows_and_count(self):
        pages = []
        for page in (1, 2, 3):
            request = self.factory.get(f"/edi835/api/client-portal/holds/{self.file.pk}/?page={page}")
            request.user = self.user
            response = held_file_claims(request, self.file.pk)
            self.assertEqual(response.status_code, 200, response.content)
            pages.append(json.loads(response.content))
        self.assertEqual([len(row["claims"]) for row in pages], [20, 20, 5])
        self.assertEqual([row["page"] for row in pages], [1, 2, 3])
        self.assertEqual([row["pages"] for row in pages], [3, 3, 3])
        self.assertNotEqual(pages[0]["claims"][0]["number"], pages[1]["claims"][0]["number"])

    def test_held_files_from_other_client_are_not_visible(self):
        request = self.factory.get(f"/edi835/api/client-portal/holds/{self.foreign.pk}/")
        request.user = self.user
        response = held_file_claims(request, self.foreign.pk)
        self.assertEqual(response.status_code, 404)
