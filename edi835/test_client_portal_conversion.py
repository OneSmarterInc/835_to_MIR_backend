"""Client User conversion must require affirmative delivery consent and tenant binding."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from edi835.client_portal_conversion import convert


class ClientUserConversionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client.objects.create(
            name="Client A", client_code="CUC-A", email="a@client.example"
        )
        self.viewer = User.objects.create_user(
            email="viewer@client.example", name="Client User",
            mobile="+14155550401", password="SomeLongDummyPassword!",
            client=self.client, client_role="CLIENT_USER",
        )
        self.admin = User.objects.create_user(
            email="admin@client.example", name="Client Admin",
            mobile="+14155550402", password="SomeLongDummyPassword!",
            client=self.client,
        )

    def request(self, actor, accepted="yes"):
        request = self.factory.post("/edi835/api/client-portal/convert/", {
            "edi_file": SimpleUploadedFile("input.835", b"ISA*00*TEST~"),
            "confirm_send": accepted,
        })
        request.user = actor
        return request

    @patch("edi835.client_portal_conversion.process_edi835_file_content")
    def test_cannot_convert_without_explicit_sftp_confirmation(self, process):
        response = convert(self.request(self.viewer, accepted="no"))
        self.assertEqual(response.status_code, 400)
        process.assert_not_called()

    @patch("edi835.client_portal_conversion.process_edi835_file_content")
    def test_conversion_receives_only_authenticated_tenant(self, process):
        processed = SimpleNamespace(
            pk="79acffcf-b0ae-4f51-a943-4be883a03e60", original_filename="input.835",
            delivered_claims_count=2, held_claims_count=1,
            mir_file=SimpleNamespace(
                mir_filename="out.mir", status="PUSHED"
            ),
        )
        process.return_value = {"success": True, "db_record": processed}
        response = convert(self.request(self.viewer))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json.loads(response.content)["sent"])
        self.assertIs(process.call_args.kwargs["client"], self.client)
        self.assertEqual(process.call_args.kwargs["original_filename"], "input.835")

    @patch("edi835.client_portal_conversion.process_edi835_file_content")
    def test_client_admin_cannot_use_restricted_client_user_endpoint(self, process):
        response = convert(self.request(self.admin))
        self.assertEqual(response.status_code, 403)
        process.assert_not_called()
