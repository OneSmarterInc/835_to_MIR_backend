"""DRF compatibility boundary for the established Django API contract.

The existing views remain the sole owners of business logic.  These adapters
move request dispatch, authentication, permissions, parsing and JSON rendering
under DRF without changing URLs or response payloads consumed by React.
"""

import json

from django.http import JsonResponse
from rest_framework import permissions, status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler

from .drf_auth import ExistingSessionAuthentication


class IsPortalUser(permissions.BasePermission):
    message = "Authentication required"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated)


class IsAdministrator(permissions.BasePermission):
    message = "Administrator access required"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_staff)


def compatible_exception_handler(exc, context):
    """Keep the legacy error envelope while using DRF exception handling."""
    response = exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        message = "Authentication required"
    elif isinstance(exc, PermissionDenied):
        message = str(getattr(exc, "detail", "Access denied"))
    else:
        detail = response.data.get("detail") if isinstance(response.data, dict) else None
        message = str(detail or "Request failed")
    response.data = {"success": False, "error": message}
    return response


def _json_response_as_drf(response):
    if not isinstance(response, JsonResponse):
        return response
    try:
        payload = json.loads(response.content.decode(response.charset or "utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return response
    converted = Response(payload, status=response.status_code)
    for header, value in response.items():
        if header.lower() not in {"content-type", "content-length"}:
            converted[header] = value
    return converted


def legacy_api(view_function, *, access="authenticated"):
    """Wrap one established Django JSON/file view in a DRF APIView."""
    if access == "public":
        # Allow anonymous access without replacing an already-authenticated
        # Django session user on request._request with AnonymousUser.
        authentication_classes = [ExistingSessionAuthentication]
        permission_classes = [permissions.AllowAny]
    elif access == "admin":
        authentication_classes = [ExistingSessionAuthentication]
        permission_classes = [IsAdministrator]
    else:
        authentication_classes = [ExistingSessionAuthentication]
        permission_classes = [IsPortalUser]

    class LegacyContractAPIView(APIView):
        parser_classes = [MultiPartParser, FormParser, JSONParser]
        renderer_classes = [JSONRenderer]
        http_method_names = ["get", "post", "put", "patch", "delete", "options", "head"]

        def get_exception_handler(self):
            return compatible_exception_handler

        def _delegate(self, request, *args, **kwargs):
            response = view_function(request._request, *args, **kwargs)
            return _json_response_as_drf(response)

        get = _delegate
        post = _delegate
        put = _delegate
        patch = _delegate
        delete = _delegate

    LegacyContractAPIView.authentication_classes = authentication_classes
    LegacyContractAPIView.permission_classes = permission_classes
    LegacyContractAPIView.__name__ = f"DRF_{view_function.__name__}"
    LegacyContractAPIView.__doc__ = view_function.__doc__
    return LegacyContractAPIView.as_view()


def public_api(view_function):
    return legacy_api(view_function, access="public")


def authenticated_api(view_function):
    return legacy_api(view_function, access="authenticated")


def admin_api(view_function):
    return legacy_api(view_function, access="admin")


class DRFStatusView(APIView):
    permission_classes = [IsAdministrator]
    authentication_classes = [ExistingSessionAuthentication]
    renderer_classes = [JSONRenderer]

    def get_exception_handler(self):
        return compatible_exception_handler

    def get(self, request):
        return Response(
            {"success": True, "api_layer": "django-rest-framework"},
            status=status.HTTP_200_OK,
        )
