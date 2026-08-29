"""DRF authentication that reuses the established Django session user."""

from rest_framework.authentication import BaseAuthentication


class ExistingSessionAuthentication(BaseAuthentication):
    """Trust the user already resolved by Django's authentication middleware."""

    def authenticate(self, request):
        user = getattr(request._request, "user", None)
        if user and user.is_authenticated:
            return user, None
        return None
