from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings
import json
import re
from accounts.admin_screens import user_can_access_screen


OFFBOARDED_PAYLOAD = {
    "success": False,
    "error": "Access denied. Contact your administrator.",
    "message": "Access denied. Contact your administrator.",
    "code": "CLIENT_OFFBOARDED",
    "offboarded": True,
}

MFA_EXEMPT_PATHS = {
    "/accounts/api/login/", "/accounts/api/logout/", "/accounts/api/user/",
    "/accounts/api/signup/", "/accounts/api/totp/setup/", "/accounts/api/totp/verify/",
}

ADMIN_SCREEN_PATHS = (
    ("/admin-panel/api/access/", "access"),
    ("/admin-panel/api/users", "access"),
    ("/accounts/api/admin/users", "access"),
    ("/admin-panel/api/audit-logs", "audit"),
    ("/admin-panel/api/default-smtp", "defaults"),
    ("/admin-panel/api/mappings", "defaults"),
    ("/edi835/api/admin/sftp-automation", "sftp-automation"),
)

def required_admin_screen(path):
    if "/offboarding/" in path:
        return "offboard"
    if "/documents" in path or path.startswith("/admin-panel/api/documents/"):
        return "docs"
    return next((screen for prefix, screen in ADMIN_SCREEN_PATHS if path.startswith(prefix)), None)


def sensitive_client_id(request, path):
    if path.startswith("/edi835/api/tracked-files") or path.startswith("/edi835/api/reconciliation"):
        return request.GET.get("client_id")
    match = re.search(r"/admin-panel/api/clients/([0-9a-f-]+)/edi-files", path)
    return match.group(1) if match else None


def requested_client_id(request, path):
    """Resolve a tenant scope from administrative and conversion requests."""
    match = re.search(r"/admin-panel/api/(?:download/|clients/)([0-9a-f-]+)(?:/|$)", path)
    if match:
        return match.group(1)
    client_id = (
        request.GET.get("client_id") or request.GET.get("client")
        or request.POST.get("client_id") or request.POST.get("client")
    )
    if client_id or request.content_type != "application/json" or not request.body:
        return client_id
    try:
        payload = json.loads(request.body.decode("utf-8"))
        return payload.get("client_id") or payload.get("client")
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def client_access_revoked(user):
    if not user or not user.is_authenticated or user.is_staff or user.is_superuser:
        return False
    client = getattr(user, "client", None)
    return bool(
        not getattr(user, "is_active", False)
        or (
            client
            and getattr(client, "stage", "") == "offboarded"
        )
    )

class AdminAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path.lower()
        normalized_path = path if path.endswith('/') else f'{path}/'

        if settings.MFA_ENFORCEMENT_ENABLED and request.user.is_authenticated and "/api/" in normalized_path and normalized_path not in MFA_EXEMPT_PATHS:
            enrolled = bool(getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None))
            if not enrolled or not request.session.get("totp_verified", False):
                return JsonResponse({"success": False, "error": "MFA enrollment and verification required."}, status=403)

        # A delegated administrator may only address a tenant while an
        # explicit, unexpired grant exists. This check is independent of the
        # optional MFA setting and cannot be bypassed by calling an API directly.
        if request.user.is_authenticated and request.user.is_staff and not request.user.is_superuser:
            from admin_panel.access_control import has_active_client_grant
            client_id = requested_client_id(request, normalized_path)
            client_path = bool(re.search(r"/admin-panel/api/(?:download/|clients/)[0-9a-f-]+(?:/|$)", normalized_path))
            client_scoped = client_path or (normalized_path.startswith("/edi835/api/") and bool(client_id))
            if client_scoped and (not client_id or not has_active_client_grant(request.user, client_id)):
                return JsonResponse({"success": False, "error": "Temporary approved client access is required.", "code": "CLIENT_GRANT_REQUIRED"}, status=403)

        # --- OFFBOARDED CLIENT BLOCK (highest priority) ---
        # Allow logout and login endpoints so user can see the error and log out
        exempt_paths = {
            '/accounts/api/login/',
            '/accounts/api/logout/',
            '/accounts/api/user/',
        }
        is_exempt = normalized_path in exempt_paths

        if not is_exempt and client_access_revoked(getattr(request, 'user', None)):
            client = getattr(request.user, 'client', None)
            if client:
                if '/api/' in path:
                    return JsonResponse(OFFBOARDED_PAYLOAD, status=403)
                return HttpResponseForbidden(
                    "Access denied. Contact your administrator."
                )

        # The administrator/mapping routes serve the React application shell.
        # Anonymous visitors must be allowed to load that shell so React can
        # display the dedicated Admin Sign In screen.  Actual admin APIs remain
        # protected, and an authenticated standard user still cannot enter an
        # administrative UI route.
        is_admin_api = path.startswith('/admin-panel/') or path.startswith('/accounts/api/admin/')
        is_admin_ui = path.startswith('/administrator') or path.startswith('/mapping')
        if is_admin_api or is_admin_ui:
            if is_admin_api and (not request.user.is_authenticated or not request.user.is_staff):
                return JsonResponse({"success": False, "error": "Access denied. Administrative privileges required."}, status=403)
            if is_admin_ui and request.user.is_authenticated and not request.user.is_staff:
                return HttpResponseForbidden("Access Denied: Standard users cannot access administrative paths.")

            screen = required_admin_screen(normalized_path)
            requested_screen = request.headers.get("X-Admin-Screen", "").strip().lower()
            if not screen and requested_screen:
                screen = requested_screen
            if is_admin_api and screen and not user_can_access_screen(request.user, screen):
                return JsonResponse({"success": False, "error": f"Access to the {screen} screen is not assigned."}, status=403)
            
            # Block administrative API access if TOTP is enabled but not verified in the session
            if request.user.is_authenticated and getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None) and not request.session.get("totp_verified", False):
                if '/api/' in path:
                    return JsonResponse({"success": False, "error": "MFA verification required."}, status=403)
                
        return self.get_response(request)

class ClientAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path.lower()
        
        # Protect general /api/ routes that aren't for accounts/login
        if path.startswith('/api/') and not path.startswith('/accounts/api/login') and not path.startswith('/accounts/api/signup') and not path.startswith('/admin-panel/'):
            if not request.user.is_authenticated:
                return JsonResponse({"success": False, "error": "Access denied. Authentication required."}, status=401)
            
            # Enforce MFA verification for standard API access if TOTP is enabled
            if getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None) and not request.session.get("totp_verified", False) and not path.startswith('/accounts/api/totp'):
                return JsonResponse({"success": False, "error": "MFA verification required."}, status=403)
                
        return self.get_response(request)
