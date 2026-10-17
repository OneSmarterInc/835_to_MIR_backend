from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings
import json
import re
from accounts.admin_screens import user_can_access_screen
from accounts.request_context import _current_request


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
        # Client Users use a narrow allowlist; shared conversion controls must
        # never permit cross-tenant source IDs, unconfirmed transmission, or
        # an ALL/837/RECON batch.
        user = getattr(request, "user", None)
        if (user and user.is_authenticated and not user.is_staff
                and getattr(user, "client_role", "") == "DEMO"):
            # Default-deny business operations. Demo shares only the tenant-scoped
            # Analysis endpoint; authentication and its own MFA remain available.
            auth_paths = {
                "/accounts/api/user/", "/accounts/api/login/", "/accounts/api/logout/",
                "/accounts/api/user/change-password/", "/accounts/api/totp/setup/",
                "/accounts/api/totp/verify/", "/accounts/api/webauthn/register/options/",
                "/accounts/api/webauthn/register/verify/", "/accounts/api/webauthn/login/options/",
                "/accounts/api/webauthn/login/verify/", "/accounts/api/webauthn/credentials/",
            }
            analysis = normalized_path == "/edi835/api/client-portal/analysis/"
            shell = request.method == "GET" and (
                normalized_path in {"/", "/analysis/"}
                or re.fullmatch(r"/analysis/claims/[^/]+/", normalized_path)
                or normalized_path.startswith(("/static/", "/assets/"))
            )
            permitted = (
                (analysis and request.method in {"GET", "POST"}) or shell
                or normalized_path in auth_paths
                or re.fullmatch(r"/accounts/api/webauthn/credentials/[0-9a-f-]+/", normalized_path)
            )
            if not permitted:
                return JsonResponse({"success": False, "error": "Demo role can only access Analysis."}, status=403)
            if analysis:
                selected_client = requested_client_id(request, normalized_path)
                if not user.client_id or (selected_client and str(selected_client) != str(user.client_id)):
                    return JsonResponse({"success": False, "error": "Client scope mismatch"}, status=403)
        if (user and user.is_authenticated and not user.is_staff
                and getattr(user, "client_role", "CLIENT_ADMIN") == "CLIENT_USER"):
            public_auth = {
                "/accounts/api/user/", "/accounts/api/login/",
                "/accounts/api/logout/", "/accounts/api/user/change-password/",
            }
            auth_prefixes = ("/accounts/api/totp/", "/accounts/api/webauthn/")
            read_paths = {
                "/edi835/api/tracked-files/",
                "/api/download/",
                "/api/start-batch-conversion/",
            }
            write_paths = {
                "/api/validate/", "/api/convert/", "/api/start-batch-conversion/",
                "/edi835/api/sftp/push/",
            }
            client_workspace = normalized_path.startswith("/edi835/api/client-portal/")
            file_preview = bool(re.fullmatch(
                r"/api/file-content/[0-9a-f-]+/", normalized_path
            ))
            permitted = (
                (client_workspace and request.method == "GET")
                or (
                    normalized_path in {
                        "/edi835/api/client-portal/actions/",
                        "/edi835/api/client-portal/convert/",
                        "/edi835/api/client-portal/analysis/",
                    } and request.method == "POST"
                )
                or (normalized_path in read_paths and request.method == "GET")
                or (file_preview and request.method == "GET")
                or (normalized_path in write_paths and request.method == "POST")
                or normalized_path in public_auth
                or any(normalized_path.startswith(p) for p in auth_prefixes)
            )
            protected = normalized_path.startswith((
                "/api/", "/accounts/", "/edi835/", "/converter/",
                "/admin-panel/", "/administrator", "/adminstrator", "/mapping",
            ))
            if protected and not permitted:
                return JsonResponse({
                    "success": False,
                    "error": "Client User role cannot access this operation.",
                }, status=403) if "/api/" in normalized_path else HttpResponseForbidden(
                    "Client User role cannot access this operation."
                )

            if (normalized_path in read_paths | write_paths or file_preview):
                client_id = str(user.client_id or "")
                if not client_id:
                    return JsonResponse({"success": False, "error": "Client assignment required"}, status=403)
                if request.method == "POST" and normalized_path in write_paths:
                    try:
                        posted = json.loads(request.body.decode("utf-8")) if request.content_type == "application/json" and request.body else {}
                    except (ValueError, TypeError, UnicodeDecodeError):
                        posted = {}
                else:
                    posted = {}
                selected_client = (posted.get("client_id") or posted.get("client")
                    or request.GET.get("client_id") or request.GET.get("client"))
                if selected_client and str(selected_client) != client_id:
                    return JsonResponse({"success": False, "error": "Client scope mismatch"}, status=403)
                file_id = (
                    posted.get("file_id") or request.GET.get("file_id")
                )
                if file_preview:
                    file_id = normalized_path.split("/")[3]
                if normalized_path == "/api/download/" and (
                    not file_id or request.GET.get("mir_content")
                ):
                    return JsonResponse({"success": False, "error": "A source file ID is required for this download"}, status=400)
                if normalized_path == "/edi835/api/sftp/push/":
                    file_id = posted.get("file_id")
                    if posted.get("force") is True:
                        return JsonResponse({"success": False, "error": "Force resend requires Client Admin"}, status=403)
                if file_id:
                    from edi835.models import EDI835File
                    try:
                        is_own_file = EDI835File.objects.filter(
                            id=file_id, client_id=user.client_id
                        ).exists()
                    except (ValueError, TypeError):
                        is_own_file = False
                    if not is_own_file:
                        return JsonResponse({"success": False, "error": "File not found for client"}, status=404)
                if normalized_path == "/api/start-batch-conversion/" and request.method == "POST":
                    if str(posted.get("automation_type") or "").upper() != "835":
                        return JsonResponse({"success": False, "error": "Only 835 batch is allowed for Client User"}, status=403)
                if request.method == "POST" and normalized_path in {
                    "/api/convert/", "/api/start-batch-conversion/",
                    "/edi835/api/sftp/push/",
                } and request.headers.get("X-FileX-Confirm-Send") != "yes":
                    return JsonResponse({
                        "success": False,
                        "error": "Confirm outbound MIR delivery before running this operation.",
                    }, status=400)


        if settings.MFA_ENFORCEMENT_ENABLED and request.user.is_authenticated and "/api/" in normalized_path and normalized_path not in MFA_EXEMPT_PATHS:
            enrolled = bool(getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None))
            if not enrolled or not request.session.get("totp_verified", False):
                return JsonResponse({"success": False, "error": "MFA enrollment and verification required."}, status=403)

        if request.user.is_authenticated and request.user.is_staff and not request.user.is_superuser:
            from admin_panel.access_control import has_active_client_grant
            client_id = requested_client_id(request, normalized_path)
            client_path = bool(re.search(r"/admin-panel/api/(?:download/|clients/)[0-9a-f-]+(?:/|$)", normalized_path))
            client_scoped = client_path or (normalized_path.startswith("/edi835/api/") and bool(client_id))
            if client_scoped and (not client_id or not has_active_client_grant(request.user, client_id)):
                return JsonResponse({"success": False, "error": "Temporary approved client access is required.", "code": "CLIENT_GRANT_REQUIRED"}, status=403)

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
            
            if request.user.is_authenticated and getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None) and not request.session.get("totp_verified", False):
                if '/api/' in path:
                    return JsonResponse({"success": False, "error": "MFA verification required."}, status=403)
                
        return self.get_response(request)

class ClientAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            path = request.path.lower()
            
            if path.startswith('/api/') and not path.startswith('/accounts/api/login') and not path.startswith('/accounts/api/signup') and not path.startswith('/admin-panel/'):
                if not request.user.is_authenticated:
                    return JsonResponse({"success": False, "error": "Access denied. Authentication required."}, status=401)
                
                if getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None) and not request.session.get("totp_verified", False) and not path.startswith('/accounts/api/totp'):
                    return JsonResponse({"success": False, "error": "MFA verification required."}, status=403)
                    
            return self.get_response(request)
        finally:
            _current_request.reset(token)
