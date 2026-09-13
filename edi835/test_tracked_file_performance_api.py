import json

from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from edi835.models import EDI835File
from edi835.tracked_file_details import conversion_hold_files, tracked_file_details
from edi835.tracked_files_eastern import tracked_files_list_eastern


class TrackedFilePerformanceApiTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.admin = User.objects.create_superuser(
            email="perf-admin@example.com",
            name="Performance Admin",
            mobile="5550000100",
            password="test-password",
        )
        self.client_a = Client.objects.create(
            name="Performance Client A",
            client_code="PERF-A",
            email="a@example.com",
        )
        self.client_b = Client.objects.create(
            name="Performance Client B",
            client_code="PERF-B",
            email="b@example.com",
        )
        self.finding = {
            "rule_code": "MP003",
            "severity": "HOLD",
            "claim_index": "1",
            "claim_number": "CLAIM100",
            "reason": "Payment exceeds the derived covered amount.",
        }
        self.file_a = EDI835File.objects.create(
            client=self.client_a,
            original_filename="client-a.835",
            stored_filename="client-a.835",
            status="ARCHIVED",
            claims_count=2,
            delivered_claims_count=1,
            held_claims_count=1,
            conversion_findings=[self.finding],
        )
        self.file_b = EDI835File.objects.create(
            client=self.client_b,
            original_filename="client-b.835",
            stored_filename="client-b.835",
            status="ARCHIVED",
            claims_count=1,
            held_claims_count=1,
            conversion_findings=[{
                **self.finding,
                "claim_number": "CLAIM200",
            }],
        )

    @staticmethod
    def _payload(response):
        return json.loads(response.content.decode("utf-8"))

    def _request(self, path, params=None):
        request = self.factory.get(path, data=params or {})
        request.user = self.admin
        return request

    def test_tracked_files_omits_conversion_findings_by_default(self):
        response = tracked_files_list_eastern(
            self._request("/edi835/api/tracked-files/")
        )
        self.assertEqual(response.status_code, 200)
        payload = self._payload(response)
        row = next(item for item in payload["files"] if item["id"] == str(self.file_a.id))
        self.assertEqual(row["held_claims_count"], 1)
        self.assertNotIn("conversion_findings", row)

    def test_tracked_files_can_still_include_findings_explicitly(self):
        response = tracked_files_list_eastern(
            self._request(
                "/edi835/api/tracked-files/",
                {"include_conversion_findings": "1"},
            )
        )
        self.assertEqual(response.status_code, 200)
        payload = self._payload(response)
        row = next(item for item in payload["files"] if item["id"] == str(self.file_a.id))
        self.assertEqual(row["conversion_findings"][0]["claim_number"], "CLAIM100")

    def test_file_details_loads_findings_only_for_requested_file(self):
        response = tracked_file_details(
            self._request(
                f"/edi835/api/tracked-files/{self.file_a.id}/details/",
                {"client_id": str(self.client_a.id)},
            ),
            self.file_a.id,
        )
        self.assertEqual(response.status_code, 200)
        payload = self._payload(response)
        self.assertEqual(payload["file"]["id"], str(self.file_a.id))
        self.assertEqual(
            payload["file"]["conversion_findings"][0]["claim_number"],
            "CLAIM100",
        )

    def test_conversion_hold_summary_is_client_scoped_and_lightweight(self):
        response = conversion_hold_files(
            self._request(
                "/edi835/api/checks/conversion-holds/",
                {"client_id": str(self.client_a.id)},
            )
        )
        self.assertEqual(response.status_code, 200)
        payload = self._payload(response)
        self.assertEqual(len(payload["files"]), 1)
        row = payload["files"][0]
        self.assertEqual(row["id"], str(self.file_a.id))
        self.assertEqual(row["conversion_issue_count"], 1)
        self.assertNotIn("conversion_findings", row)
