"""Regression tests for the backward-compatible DRF API boundary."""

from django.test import TestCase
from django.urls import resolve
from rest_framework.test import APIClient
from rest_framework.views import APIView

from accounts.models import Client, User
from accounts.serializers import UserSerializer
from edi835.models import EDI835File, MIRFile, SFTPConfig
from edi835.serializers import EDI835FileSerializer, SFTPConfigSerializer


class DRFCompatibilityContractTests(TestCase):
    def setUp(self):
        self.http = APIClient()
        self.admin = User.objects.create_superuser(
            email="drf-admin@example.com",
            name="DRF Admin",
            mobile="+15559876",
            password="correct-password",
        )
        self.portal_client = Client.objects.create(
            name="DRF Client",
            client_code="DRF-CLIENT",
            email="drf-client@example.com",
            mir_filename_format="CLAIMS_YYYYMMDD_hhmmss.MIR",
        )

    def test_existing_urls_are_dispatched_by_drf_views(self):
        for url in (
            "/accounts/api/login/",
            "/accounts/api/user/",
            "/api/validate/",
            "/edi835/api/metrics/",
            "/admin-panel/api/clients/",
        ):
            callback = resolve(url).func
            self.assertTrue(issubclass(callback.view_class, APIView), url)

    def test_admin_drf_status_is_protected_and_uses_legacy_envelope(self):
        denied = self.http.get("/api/drf/status/")
        self.assertIn(denied.status_code, (401, 403))
        self.assertFalse(denied.json()["success"])

        self.http.force_login(self.admin)
        allowed = self.http.get("/api/drf/status/")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json(), {
            "success": True,
            "api_layer": "django-rest-framework",
        })

    def test_existing_user_info_response_shape_is_preserved(self):
        response = self.http.get("/accounts/api/user/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("authenticated", response.json())

    def test_serializers_do_not_expose_authentication_or_sftp_secrets(self):
        user_data = UserSerializer(self.admin).data
        self.assertNotIn("password", user_data)
        self.assertNotIn("totp_secret", user_data)
        self.assertNotIn("recovery_codes", user_data)

        config = SFTPConfig.objects.create(
            client=self.portal_client,
            username="integration-user",
            password="inbound-secret",
            ssh_key="private-key",
            outbound_password="outbound-secret",
            outbound_ssh_key="outbound-private-key",
        )
        config_data = SFTPConfigSerializer(config).data
        for secret in ("password", "ssh_key", "outbound_password", "outbound_ssh_key"):
            self.assertNotIn(secret, config_data)

    def test_canonical_mir_filename_is_the_serialized_filename(self):
        source = EDI835File.objects.create(
            client=self.portal_client,
            original_filename="input.835",
            stored_filename="internal-storage-name.835",
            output_path="media/edi835/output/internal-storage-name.mir",
        )
        canonical = "CLAIMS_20260829_185806.MIR"
        MIRFile.objects.create(
            source_835=source,
            client=self.portal_client,
            mir_filename=canonical,
            original_835_filename=source.original_filename,
            file_content="MIR DATA",
            file_hash="f" * 64,
        )
        self.assertEqual(EDI835FileSerializer(source).data["mir_filename"], canonical)
