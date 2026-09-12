import json
from types import SimpleNamespace

from django.test import RequestFactory, TestCase

from accounts.models import Client
from edi835.models import EDI835File
from edi835.ui_change_token import api_ui_change_token


class UIChangeTokenTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client_a = Client.objects.create(
            name="Token Client A",
            client_code="TOKEN-A",
            email="a@example.com",
        )
        self.client_b = Client.objects.create(
            name="Token Client B",
            client_code="TOKEN-B",
            email="b@example.com",
        )
        self.user = SimpleNamespace(
            is_staff=True,
            is_superuser=True,
            client_id=None,
        )

    def _token(self, client_id):
        request = self.factory.get(
            "/edi835/api/ui-change-token/",
            {"client_id": str(client_id)},
        )
        request.user = self.user
        response = api_ui_change_token(request)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content.decode("utf-8"))["token"]

    def test_token_is_stable_until_selected_client_data_changes(self):
        initial = self._token(self.client_a.id)
        self.assertEqual(initial, self._token(self.client_a.id))

        EDI835File.objects.create(
            client=self.client_b,
            original_filename="other-client.835",
            stored_filename="other-client.835",
        )
        self.assertEqual(initial, self._token(self.client_a.id))

        EDI835File.objects.create(
            client=self.client_a,
            original_filename="selected-client.835",
            stored_filename="selected-client.835",
        )
        changed = self._token(self.client_a.id)
        self.assertNotEqual(initial, changed)
        self.assertEqual(changed, self._token(self.client_a.id))

    def test_conversion_finding_json_change_changes_token(self):
        source = EDI835File.objects.create(
            client=self.client_a,
            original_filename="held.835",
            stored_filename="held.835",
            held_claims_count=1,
            conversion_findings=[{
                "rule_code": "MP003",
                "severity": "HOLD",
                "claim_number": "CLAIM100",
                "reason": "Held for review.",
            }],
        )
        before = self._token(self.client_a.id)

        findings = list(source.conversion_findings)
        findings[0]["seven_day_hold_alert_count"] = 1
        EDI835File.objects.filter(id=source.id).update(conversion_findings=findings)

        after = self._token(self.client_a.id)
        self.assertNotEqual(before, after)
