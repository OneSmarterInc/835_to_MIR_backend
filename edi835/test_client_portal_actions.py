"""Tenant and workflow verification for Client Portal actions."""
import json
from decimal import Decimal
from django.test import RequestFactory, TestCase
from accounts.models import Client, User
from edi835.models import ClientPortalAction, EDI835File, EDI835Claim
from edi835.client_portal_actions import actions, review_action


class ClientPortalActionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.a = Client.objects.create(name="Client A", client_code="PA-T", email="a@example.org")
        self.b = Client.objects.create(name="Client B", client_code="PB-T", email="b@example.org")
        self.viewer = User.objects.create_user(
            email="view@client.example", name="Viewer", mobile="+14155550110",
            password="DummyPassword123!", client=self.a, client_role="CLIENT_USER",
        )
        self.admin = User.objects.create_user(
            email="admin@client.example", name="Client Admin", mobile="+14155550111",
            password="DummyPassword123!", client=self.a, client_role="CLIENT_ADMIN",
        )
        self.file_a = EDI835File.objects.create(
            client=self.a, original_filename="a.835", stored_filename="a.835",
        )
        self.file_b = EDI835File.objects.create(
            client=self.b, original_filename="b.835", stored_filename="b.835",
        )
        self.claim_b = EDI835Claim.objects.create(
            edi_file=self.file_b, claim_sequence=1, highmark_claim_number="SENSITIVE-B",
            total_charge_amount=Decimal("10.00"),
        )

    def post(self, user, payload):
        request = self.factory.post("/edi835/api/client-portal/actions/",
            data=json.dumps(payload), content_type="application/json")
        request.user = user
        return actions(request)

    def test_other_tenant_file_is_not_available(self):
        response = self.post(self.viewer, {
            "kind": "NOTE", "file_id": str(self.file_b.pk), "message": "Wrong tenant"
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClientPortalAction.objects.count(), 0)

    def test_cross_tenant_claim_is_not_accepted(self):
        response = self.post(self.viewer, {
            "kind": "NOTE", "file_id": str(self.file_a.pk),
            "claim_id": self.claim_b.pk, "message": "Wrong claim"
        })
        self.assertEqual(response.status_code, 404)

    def test_client_admin_cannot_forge_request(self):
        response = self.post(self.admin, {
            "kind": "NOTE", "file_id": str(self.file_a.pk), "message": "Admin pretending"
        })
        self.assertEqual(response.status_code, 403)

    def test_note_is_stored_and_visible_to_client_admin(self):
        response = self.post(self.viewer, {
            "kind": "NOTE", "file_id": str(self.file_a.pk), "message": "Please review this payer response"
        })
        self.assertEqual(response.status_code, 201, response.content)
        request = self.factory.get("/edi835/api/client-portal/actions/")
        request.user = self.admin
        rows = json.loads(actions(request).content)["actions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "Please review this payer response")
        review_req = self.factory.post("/edi835/api/client-portal/actions/%s/review/" % rows[0]["id"])
        review_req.user = self.admin
        response = review_action(review_req, rows[0]["id"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ClientPortalAction.objects.get(pk=rows[0]["id"]).status, "REVIEWED")
