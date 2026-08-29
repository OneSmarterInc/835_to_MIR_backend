from functools import wraps
import logging

from django.http import JsonResponse


logger = logging.getLogger(__name__)


def json_api_errors(view_function):
    """Guarantee JSON for unexpected API failures while logging the traceback."""

    @wraps(view_function)
    def wrapped_view(request, *args, **kwargs):
        try:
            return view_function(request, *args, **kwargs)
        except Exception as exc:
            logger.exception("Unhandled API failure in %s", view_function.__name__)
            return JsonResponse(
                {
                    "success": False,
                    "error": f"Batch pipeline failed: {exc}",
                },
                status=500,
            )

    return wrapped_view


def admin_api_required(view_function):
    """
    Allow only authenticated staff/admin users.

    Assumes the project's authentication middleware has resolved
    the Token header and populated request.user.
    """

    @wraps(view_function)
    def wrapped_view(request, *args, **kwargs):
        user = getattr(
            request,
            "user",
            None,
        )

        if (
            not user
            or not user.is_authenticated
            or not user.is_staff
        ):
            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "Administrator access required"
                    ),
                },
                status=403,
            )

        return view_function(
            request,
            *args,
            **kwargs,
        )

    return wrapped_view

def authenticated_api_required(view_function):
    """Allow authenticated portal users and staff administrators."""

    @wraps(view_function)
    def wrapped_view(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Authentication required",
                },
                status=401,
            )
        from project835.middleware import OFFBOARDED_PAYLOAD, client_access_revoked
        if client_access_revoked(user):
            return JsonResponse(OFFBOARDED_PAYLOAD, status=403)
        return view_function(request, *args, **kwargs)

    return wrapped_view
