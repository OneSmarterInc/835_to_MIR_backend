from django.http import JsonResponse, HttpResponseForbidden

class AdminAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path.lower()

        # --- OFFBOARDED CLIENT BLOCK (highest priority) ---
        # Allow logout and login endpoints so user can see the error and log out
        EXEMPT_PATHS = ['/accounts/api/login', '/accounts/api/logout', '/accounts/api/user-info']
        is_exempt = any(path.startswith(p) for p in EXEMPT_PATHS)

        if not is_exempt and request.user.is_authenticated and not request.user.is_staff and not request.user.is_superuser:
            client = getattr(request.user, 'client', None)
            if client and getattr(client, 'stage', '') == 'offboarded':
                if '/api/' in path:
                    return JsonResponse({
                        "success": False,
                        "error": f"ACCESS DENIED: {client.name} has been offboarded. Contact the administrator for assistance.",
                        "offboarded": True
                    }, status=403)
                return HttpResponseForbidden(
                    f"ACCESS DENIED: {client.name} has been offboarded. Contact the administrator for assistance."
                )

        # Protect all admin-panel api calls and UI paths (administrator/mapping)
        if path.startswith('/admin-panel/') or path.startswith('/administrator') or path.startswith('/mapping') or path.startswith('/accounts/api/admin/'):
            if not request.user.is_authenticated or not request.user.is_staff:
                if '/api/' in path or '/api/' in request.path.lower():
                    return JsonResponse({"success": False, "error": "Access denied. Administrative privileges required."}, status=403)
                return HttpResponseForbidden("Access Denied: Standard users cannot access administrative paths.")
            
            # Block administrative API access if TOTP is enabled but not verified in the session
            if getattr(request.user, "totp_enabled", False) and getattr(request.user, "totp_secret", None) and not request.session.get("totp_verified", False):
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
