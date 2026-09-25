# 2026-09-23 - Yash: Comprehensive CSRF protection test suite for Task 6
import json
from django.test import TestCase, Client
from django.urls import get_resolver, URLPattern, URLResolver
from django.contrib.auth import get_user_model
from accounts.models import Client as PortalClient

User = get_user_model()


class CSRFProtectionTests(TestCase):
    """Test suite verifying CSRF enforcement across browser session and DRF endpoints."""

    def setUp(self):
        self.portal_client = PortalClient.objects.create(
            name="CSRF Test Organization",
            client_code="CSRF-CLT-01",
            email="contact@csrftest.com",
            status="ACTIVE",
        )
        self.admin_user = User.objects.create_user(
            email="csrf_admin@onesmarter.com",
            name="CSRF Admin",
            mobile="+15550001111",
            password="TestPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.normal_user = User.objects.create_user(
            email="csrf_user@onesmarter.com",
            name="CSRF Normal User",
            mobile="+15550002222",
            password="TestPassword123!",
            is_staff=False,
            is_superuser=False,
            client=self.portal_client,
        )
        self.client = Client(enforce_csrf_checks=True)

    def test_bootstrap_endpoint_sets_csrf_cookie(self):
        """Verify GET /accounts/api/user/ sets the csrftoken cookie."""
        response = self.client.get("/accounts/api/user/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertTrue(len(response.cookies["csrftoken"].value) > 0)

    def test_api_login_without_token_returns_403(self):
        """Verify POST to api_login without CSRF token returns 403 Forbidden."""
        response = self.client.post(
            "/accounts/api/login/",
            data=json.dumps({"email": self.normal_user.email, "password": "TestPassword123!"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_api_login_with_bootstrap_token_succeeds(self):
        """Verify POST to api_login with bootstrap CSRF token proceeds."""
        bootstrap_res = self.client.get("/accounts/api/user/")
        csrf_token = bootstrap_res.cookies["csrftoken"].value

        response = self.client.post(
            "/accounts/api/login/",
            data=json.dumps({"email": self.normal_user.email, "password": "TestPassword123!"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertIn(response.status_code, [200, 302])

    def test_drf_view_under_existing_session_auth_enforces_csrf(self):
        """Verify DRF APIViews under ExistingSessionAuthentication reject tokenless POST with 403."""
        self.client.force_login(self.admin_user)
        # Attempt tokenless POST to authenticated DRF endpoint
        response = self.client.post(
            "/accounts/api/admin/clients/create/",
            data=json.dumps({"name": "Test CSRF Client"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_all_state_changing_endpoints_reject_tokenless_post(self):
        """Iterate all URL patterns and assert zero offenders for tokenless state-changing requests."""
        self.client.force_login(self.admin_user)
        session = self.client.session
        session["totp_verified"] = True
        session.save()

        EXEMPT_VIEW_NAMES = set()  # Documented exempt views if any

        urls = []

        def collect_urls(patterns, prefix=""):
            for pattern in patterns:
                if isinstance(pattern, URLPattern):
                    url_str = prefix + str(pattern.pattern)
                    clean_path = "/" + url_str.lstrip("^/").rstrip("$")
                    import re
                    clean_path = re.sub(r"<[^>]+>", "1", clean_path)
                    clean_path = re.sub(r"\(.*?\)", "1", clean_path)
                    if not clean_path.startswith("/"):
                        clean_path = "/" + clean_path
                    urls.append((clean_path, pattern.name or ""))
                elif isinstance(pattern, URLResolver):
                    collect_urls(pattern.url_patterns, prefix + str(pattern.pattern))

        resolver = get_resolver()
        collect_urls(resolver.url_patterns)

        offenders = []
        for path, view_name in urls:
            if view_name in EXEMPT_VIEW_NAMES:
                continue
            for method in ("post", "put", "patch", "delete"):
                response = getattr(self.client, method)(path, data="{}", content_type="application/json")
                if response.status_code not in (403, 404, 405, 301, 302):
                    offenders.append((method.upper(), path, response.status_code))

        self.assertEqual(offenders, [], f"Endpoints accepting tokenless requests: {offenders}")
