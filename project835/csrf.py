"""Keep CSRF rejections readable to API clients without weakening enforcement."""

from django.http import JsonResponse
from django.views.csrf import csrf_failure as html_csrf_failure


def csrf_failure(request, reason=""):
    if request.path.startswith(("/api/", "/accounts/api/", "/edi835/api/", "/admin-panel/api/")):
        return JsonResponse({
            "success": False,
            "code": "CSRF_FAILED",
            "error": "CSRF verification failed. Refresh the page and try again.",
        }, status=403)
    return html_csrf_failure(request, reason=reason)
