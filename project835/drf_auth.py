"""DRF authentication that reuses the established Django session user and enforces CSRF protection."""

# 2026-09-23 - Yash: Enforce CSRF checking on all unsafe HTTP methods for DRF session authentication
from rest_framework.authentication import SessionAuthentication


class ExistingSessionAuthentication(SessionAuthentication):
    """Trust the user already resolved by Django's authentication middleware and enforce CSRF."""

    def authenticate(self, request):
        if getattr(request._request, "_dont_enforce_csrf_checks", False):
            user = getattr(request._request, "user", None)
            if user and user.is_authenticated:
                return user, None
            return None

        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            self.enforce_csrf(request)

        user = getattr(request._request, "user", None)
        if user and user.is_authenticated:
            return user, None
        return None
