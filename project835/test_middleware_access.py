import json
from types import SimpleNamespace

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from project835.middleware import AdminAccessMiddleware


@override_settings(MFA_ENFORCEMENT_ENABLED=True)
class AdminClientGrantMiddlewareTests(SimpleTestCase):
    def _request(self, *, superuser):
        request = RequestFactory().get("/edi835/api/tracked-files/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_staff=True,
            is_superuser=superuser,
            totp_enabled=True,
            totp_secret="configured",
        )
        request.session = {"totp_verified": True}
        return request

    def test_super_admin_can_read_system_wide_tracked_files(self):
        middleware = AdminAccessMiddleware(
            lambda request: JsonResponse({"success": True})
        )

        response = middleware(self._request(superuser=True))

        self.assertEqual(response.status_code, 200)

    def test_regular_admin_still_requires_a_client_grant(self):
        middleware = AdminAccessMiddleware(
            lambda request: JsonResponse({"success": True})
        )

        response = middleware(self._request(superuser=False))

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["code"], "CLIENT_GRANT_REQUIRED")


@override_settings(MFA_ENFORCEMENT_ENABLED=True)
class AdminMFASessionLifetimeTests(SimpleTestCase):
    def test_verified_mfa_remains_valid_for_the_authenticated_session(self):
        request = RequestFactory().post("/api/files/delete/")
        request.user = SimpleNamespace(
            is_authenticated=True,
            is_staff=False,
            is_superuser=False,
            totp_enabled=True,
            totp_secret="configured",
            client=None,
        )
        # A timestamp is deliberately absent: session verification, rather
        # than a second five-minute timer, is now the source of truth.
        request.session = {"totp_verified": True}
        middleware = AdminAccessMiddleware(
            lambda current_request: JsonResponse({"success": True})
        )

        response = middleware(request)

        self.assertEqual(response.status_code, 200)
