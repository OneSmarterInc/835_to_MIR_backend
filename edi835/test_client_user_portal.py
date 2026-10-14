"""Client User tenant isolation and least-privilege regression tests."""
import json
from decimal import Decimal
from django.test import RequestFactory, TestCase, override_settings
from django.http import JsonResponse

from accounts.models import Client, User
from edi835.models import EDI835File, EDI835Claim
from edi835.client_portal_views import overview
from edi835.client_portal_claim import detail
from project835.middleware import AdminAccessMiddleware


class ClientPortalSecurityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.a = Client.objects.create(name="Alpha", client_code="ALPHA_TEST", email="a@example.org")
        self.b = Client.objects.create(name="Beta", client_code="BETA_TEST", email="b@example.org")
        self.viewer = User.objects.create_user(
            email="viewer@example.org", name="Viewer", mobile="+15555551011",
            password="SomeLongTestPassword!9", client=self.a, client_role="CLIENT_USER",
        )
        self.legacy = User.objects.create_user(
            email="admin@example.org", name="Client Admin", mobile="+15555551012",
            password="SomeLongTestPassword!9", client=self.a,
        )
        self.fa = EDI835File.objects.create(
            client=self.a, original_filename="alpha.edi", stored_filename="alpha.edi", claims_count=1,
        )
        self.fb = EDI835File.objects.create(
            client=self.b, original_filename="beta.edi", stored_filename="beta.edi", claims_count=50,
        )
        self.ca = EDI835Claim.objects.create(
            edi_file=self.fa, claim_sequence=1, highmark_claim_number="ALPHA-001",
            total_charge_amount=Decimal("100.00"), paid_amount=Decimal("60.00"),
        )
        self.cb = EDI835Claim.objects.create(
            edi_file=self.fb, claim_sequence=1, highmark_claim_number="BETA-001",
            total_charge_amount=Decimal("100.00"), paid_amount=Decimal("50.00"),
        )

    def request(self, user, url):
        req = self.factory.get(url)
        req.user = user
        return req

    def test_overview_never_counts_other_clients(self):
        response = overview(self.request(self.viewer, "/edi835/api/client-portal/overview/"))
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)
        self.assertEqual(result["client"], self.a.name)
        self.assertEqual(result["metrics"]["received_claims"], 1)

    def test_claim_from_another_client_returns_404(self):
        response = detail(
            self.request(self.viewer, "/edi835/api/client-portal/claim/%s/" % self.cb.pk),
            self.cb.pk,
        )
        self.assertEqual(response.status_code, 404)

    def test_own_claim_is_visible(self):
        response = detail(
            self.request(self.viewer, "/edi835/api/client-portal/claim/%s/" % self.ca.pk),
            self.ca.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["claim"]["number"], "ALPHA-001")

    def test_client_admin_does_not_impersonate_client_user(self):
        response = overview(self.request(self.legacy, "/edi835/api/client-portal/overview/"))
        self.assertEqual(response.status_code, 403)

    @override_settings(MFA_ENFORCEMENT_ENABLED=False)
    def test_client_user_cannot_reach_conversion_api(self):
        middleware = AdminAccessMiddleware(lambda _: JsonResponse({"ok": True}))
        req = self.factory.post("/edi835/api/start-batch-conversion/")
        req.user = self.viewer
        self.assertEqual(middleware(req).status_code, 403)

    @override_settings(MFA_ENFORCEMENT_ENABLED=False)
    def test_client_user_cannot_post_to_review_api(self):
        middleware = AdminAccessMiddleware(lambda _: JsonResponse({"ok": True}))
        req = self.factory.post("/edi835/api/client-portal/overview/")
        req.user = self.viewer
        self.assertEqual(middleware(req).status_code, 403)
