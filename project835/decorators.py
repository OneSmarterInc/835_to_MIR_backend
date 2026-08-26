from functools import wraps

from django.http import JsonResponse


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
        return view_function(request, *args, **kwargs)

    return wrapped_view
