from django.http import JsonResponse, HttpResponseForbidden


OFFBOARDED_PAYLOAD = {
    "success": False,
    "error": "Access denied. Contact your administrator.",
    "message": "Access denied. Contact your administrator.",
    "code": "CLIENT_OFFBOARDED",
    "offboarded": True,
}


def client_access_revoked(user):
    if not user or not user.is_authenticated or user.is_staff or user.is_superuser:
        return False
    client = getattr(user, "client", None)
    return bool(
        not getattr(user, "is_active", False)
        or (
            client
            and (
                getattr(client, "stage", "") == "offboarded"
                or getattr(client, "status", "") == "INACTIVE"
            )
        )
    )

class AdminAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path.lower()

        # --- OFFBOARDED CLIENT BLOCK (highest priority) ---
        # Allow logout and login endpoints so user can see the error and log out
        exempt_paths = {
            '/accounts/api/login/',
            '/accounts/api/logout/',
            '/accounts/api/user/',
        }
        normalized_path = path if path.endswith('/') else f'{path}/'
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
