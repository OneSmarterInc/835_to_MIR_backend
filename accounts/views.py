import base64
import io
import secrets
import logging

import pyotp
import qrcode

logger = logging.getLogger("accounts")

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from .forms import SignupForm, LoginForm


OFFBOARDED_ERROR = "Access denied. Contact your administrator."


def _is_client_access_revoked(user):
    """Return True only for portal users belonging to an offboarded client."""
    if not user or user.is_staff or user.is_superuser:
        return False
    client = getattr(user, "client", None)
    return bool(
        client
        and (
            getattr(client, "stage", "") == "offboarded"
            or getattr(client, "status", "") == "INACTIVE"
        )
    )


def _offboarded_response(user):
    client = getattr(user, "client", None)
    return JsonResponse(
        {
            "success": False,
            "error": OFFBOARDED_ERROR,
            "message": OFFBOARDED_ERROR,
            "code": "CLIENT_OFFBOARDED",
            "offboarded": True,
            "client": getattr(client, "name", "Client"),
        },
        status=403,
    )


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            request.session["totp_setup_required"] = True
            return redirect("totp_setup")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        if not request.user.totp_enabled:
            return redirect("totp_setup")
        if not request.session.get("totp_verified", False):
            return redirect("totp_verify")
        return redirect("home")

    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            user = form.user
            
            # Block offboarded client users
            if _is_client_access_revoked(user):
                messages.error(request, f"ACCESS DENIED: {user.client.name} has been offboarded. Contact the administrator for assistance.")
                return render(request, "accounts/login.html", {"form": form})
            
            login(request, user)
            if not user.totp_enabled:
                request.session["totp_setup_required"] = True
                return redirect("totp_setup")

            request.session["totp_verified"] = False
            return redirect("totp_verify")
    else:
        form = LoginForm()

    return render(request, "accounts/login.html", {"form": form})


@login_required
def totp_setup_view(request):
    user = request.user
    if user.totp_enabled:
        return redirect("home")

    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        user.save(update_fields=["totp_secret"])

    secret = user.totp_secret
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.email, issuer_name="Project835")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image()

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode()

    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        if totp.verify(code):
            user.totp_enabled = True
            recovery_codes = [secrets.token_hex(4).upper() for _ in range(10)]
            user.recovery_codes = recovery_codes
            user.save(update_fields=["totp_enabled", "recovery_codes"])

            request.session["totp_verified"] = True
            request.session["totp_setup_required"] = False
            messages.success(request, "Authenticator successfully configured.")

            return render(
                request,
                "accounts/totp_setup.html",
                {
                    "qr_code": qr_code,
                    "secret": secret,
                    "verified": True,
                    "recovery_codes": recovery_codes,
                },
            )
        else:
            messages.error(request, "Invalid authenticator code.")

    return render(
        request,
        "accounts/totp_setup.html",
        {
            "qr_code": qr_code,
            "secret": secret,
            "verified": False,
        },
    )


@login_required
def totp_verify_view(request):
    user = request.user
    if not user.totp_enabled:
        return redirect("totp_setup")

    if request.session.get("totp_verified", False):
        return redirect("home")

    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(code):
            request.session["totp_verified"] = True
            messages.success(request, "Authentication successful.")
            return redirect("home")

        messages.error(request, "Invalid authenticator code.")

    return render(request, "accounts/totp_verify.html")


def logout_view(request):
    logout(request)
    return redirect("login")


from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json

def api_user_info(request):
    if not request.user.is_authenticated:
        return JsonResponse({
            "authenticated": False,
            "user": None
        })

    # TOTP is configured only when enrollment is enabled and this user has
    # their own authenticator secret. This sends incomplete/new users to the
    # QR-code setup screen instead of the verification screen.
    totp_enabled = bool(
        getattr(request.user, "totp_enabled", False)
        and getattr(request.user, "totp_secret", None)
    )
    totp_verified = request.session.get("totp_verified", False)
    first_login = getattr(request.user, "first_login", True)

    user_name = getattr(request.user, "name", request.user.email)
    user_email = request.user.email

    if request.user.is_superuser:
        role_str = "Super Admin"
    elif request.user.is_staff:
        role_str = "Admin"
    else:
        role_str = "User"

    client_str = request.user.client.name if (request.user.client and not request.user.is_staff and not request.user.is_superuser) else "OneSmarter"

    # Check if client is offboarded
    is_offboarded = False
    if _is_client_access_revoked(request.user):
        is_offboarded = True

    return JsonResponse({
        "authenticated": True,
        "offboarded": is_offboarded,
        "offboarded_message": f"ACCESS DENIED: {client_str} has been offboarded. Contact the administrator for assistance." if is_offboarded else None,
        "user": {
            "name": user_name,
            "email": user_email,
            "is_staff": request.user.is_staff,
            "is_superuser": request.user.is_superuser,
            "role": role_str,
            "client": client_str,
            "totp_enabled": totp_enabled,
            "totp_verified": totp_verified,
            "first_login": first_login
        }
    })

@csrf_exempt
def api_login(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    # Disabled users cannot pass Django's authentication backend. Verify the
    # supplied password before returning the explicit offboarding state so the
    # response cannot be used to enumerate client accounts.
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if email and password:
        from .models import User
        candidate = User.objects.select_related("client").filter(email__iexact=email).first()
        if candidate and candidate.check_password(password) and _is_client_access_revoked(candidate):
            logger.warning("Blocked login for offboarded client user '%s'.", candidate.email)
            return _offboarded_response(candidate)

    form = LoginForm(data)
    if form.is_valid():
        user = form.user
        
        # Enforce route restrictions
        is_admin_route = bool(data.get("isAdminRoute", False))
        is_user_staff = bool(user.is_staff or user.is_superuser)
        
        if is_admin_route and not is_user_staff:
            logger.warning(f"Auth failure: Non-staff user '{user.email}' attempted admin login.")
            return JsonResponse({
                "success": False,
                "error": "Access Denied: Standard user credentials cannot be used for administrator login."
            }, status=400)
            
        if not is_admin_route and is_user_staff:
            logger.warning(f"Auth failure: Staff user '{user.email}' attempted standard login.")
            return JsonResponse({
                "success": False,
                "error": "Access Denied: Administrative credentials cannot be used for standard user login."
            }, status=400)

        # Block login for users whose client has been offboarded
        if _is_client_access_revoked(user):
            logger.warning(f"Auth failure: User '{user.email}' blocked because client '{user.client.name}' is offboarded.")
            return _offboarded_response(user)

        login(request, user)
        logger.info(f"Auth success: User '{user.email}' successfully logged in (2FA pending).")
        
        # A user is only truly TOTP-ready when BOTH totp_enabled AND totp_secret are set.
        # Users created via createsuperuser have totp_enabled=True (model default) but no secret.
        totp_enabled = getattr(user, "totp_enabled", False) and bool(user.totp_secret)
        if totp_enabled:
            request.session["totp_verified"] = False
            request.session["totp_setup_required"] = False
            next_step = "totp_verify"
        else:
            request.session["totp_verified"] = False
            request.session["totp_setup_required"] = True
            next_step = "totp_setup"

        if user.is_superuser:
            role_str = "Super Admin"
        elif user.is_staff:
            role_str = "Admin"
        else:
            role_str = "User"

        client_str = user.client.name if (user.client and not user.is_staff and not user.is_superuser) else "OneSmarter"

        # Audit Logging
        from admin_panel.models import log_audit_event
        log_audit_event(
            module="AUTH",
            action="LOGIN_INIT",
            details=f"User '{user.email}' logged in (2FA verification pending).",
            performed_by=user.name or user.email,
            client=user.client
        )

        return JsonResponse({
            "success": True,
            "next": next_step,
            "totp_enabled": totp_enabled,
            "totp_verified": False,
            "user": {
                "name": getattr(user, "name", user.email),
                "email": user.email,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                "role": role_str,
                "client": client_str
            }
        })

    errors = []
    if form.non_field_errors():
        errors.extend(form.non_field_errors())
    for field, field_errs in form.errors.items():
        if field != "__all__":
            errors.extend(field_errs)

    email_attempt = data.get("email", "") if isinstance(data, dict) else ""
    logger.warning(f"Auth failure: Login failed for '{email_attempt}'. Errors: {errors}")

    return JsonResponse({
        "success": False,
        "error": errors[0] if errors else "Invalid login credentials."
    }, status=400)

@csrf_exempt
def api_signup(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    form = SignupForm(data)
    if form.is_valid():
        user = form.save()
        login(request, user)
        request.session["totp_setup_required"] = True

        # Audit Logging
        from admin_panel.models import log_audit_event
        log_audit_event(
            module="AUTH",
            action="SIGNUP",
            details=f"New user registration for '{user.email}' (Client: {user.client.name if user.client else 'None'}).",
            performed_by=user.name or user.email,
            client=user.client
        )

        return JsonResponse({
            "success": True,
            "next": "totp_setup",
            "user": {"name": getattr(user, "name", user.email), "email": user.email}
        })

    field_errors = {}
    for field, err_list in form.errors.items():
        field_errors[field] = err_list[0] if err_list else "Invalid value."

    return JsonResponse({
        "success": False,
        "errors": field_errors,
        "error": form.non_field_errors()[0] if form.non_field_errors() else "Registration failed. Please check inputs."
    }, status=400)

@csrf_exempt
@login_required
def api_totp_setup(request):
    user = request.user
    if user.totp_enabled and not request.session.get("totp_setup_required", False):
        return JsonResponse({"verified": True, "already_configured": True})

    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        user.save(update_fields=["totp_secret"])

    secret = user.totp_secret
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.email, issuer_name="Project835")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image()

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode()

    if request.method == "POST":
        try:
            data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
        except Exception:
            data = request.POST

        code = data.get("code", "").strip()
        if totp.verify(code):
            user.totp_enabled = True
            recovery_codes = [secrets.token_hex(4).upper() for _ in range(10)]
            user.recovery_codes = recovery_codes
            user.save(update_fields=["totp_enabled", "recovery_codes"])

            request.session["totp_verified"] = True
            request.session["totp_setup_required"] = False

            # Audit Logging
            from admin_panel.models import log_audit_event
            log_audit_event(
                module="AUTH",
                action="TOTP_SETUP",
                details=f"User '{user.email}' successfully configured 2FA (TOTP).",
                performed_by=user.name or user.email,
                client=user.client
            )

            return JsonResponse({
                "success": True,
                "verified": True,
                "recovery_codes": recovery_codes,
                "message": "Authenticator successfully configured."
            })
        else:
            return JsonResponse({"success": False, "error": "Invalid authenticator code."}, status=400)

    return JsonResponse({
        "success": True,
        "qr_code": qr_code,
        "secret": secret,
        "verified": False,
    })

@csrf_exempt
@login_required
def api_totp_verify(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed."}, status=405)

    user = request.user
    if not user.totp_enabled or not user.totp_secret:
        return JsonResponse({"error": "2FA setup required. Please set up your authenticator app first.", "next": "totp_setup"}, status=400)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    code = data.get("code", "").strip()
    totp = pyotp.TOTP(user.totp_secret)
    if totp.verify(code):
        request.session["totp_verified"] = True

        # Audit Logging
        from admin_panel.models import log_audit_event
        log_audit_event(
            module="AUTH",
            action="LOGIN_SUCCESS",
            details=f"User '{user.email}' successfully authenticated (2FA verified).",
            performed_by=user.name or user.email,
            client=user.client
        )

        return JsonResponse({
            "success": True,
            "next": "home",
            "message": "Authentication successful."
        })

    return JsonResponse({"success": False, "error": "Invalid authenticator code."}, status=400)

@csrf_exempt
def api_logout(request):
    user = request.user
    user_name = "System"
    client_obj = None
    if user and user.is_authenticated:
        user_name = user.name or user.email
        client_obj = getattr(user, "client", None)

    logout(request)

    # Audit Logging
    from admin_panel.models import log_audit_event
    log_audit_event(
        module="AUTH",
        action="LOGOUT",
        details=f"User '{user_name}' logged out.",
        performed_by=user_name,
        client=client_obj
    )

    return JsonResponse({"success": True})


# ==========================================
# ADMIN CLIENT MANAGEMENT API ENDPOINTS
# ==========================================

from .models import Client, User
from edi835.models import EDI835File
from django.db.models import Count

@csrf_exempt
def api_admin_clients(request):
    """
    GET /accounts/api/admin/clients/
    Returns list of all clients, with optional search and status filtering.
    """
    search_q = request.GET.get("search", "").strip()
    status_q = request.GET.get("status", "").strip()

    clients_qs = Client.objects.annotate(users_count=Count("users", distinct=True))

    if search_q:
        clients_qs = clients_qs.filter(
            models.Q(name__icontains=search_q) |
            models.Q(client_code__icontains=search_q) |
            models.Q(email__icontains=search_q) |
            models.Q(phone__icontains=search_q)
        )

    if status_q and status_q.upper() in ["ACTIVE", "INACTIVE"]:
        clients_qs = clients_qs.filter(status=status_q.upper())

    total_clients = Client.objects.count()
    active_clients = Client.objects.filter(status="ACTIVE").count()
    inactive_clients = Client.objects.filter(status="INACTIVE").count()

    clients_data = []
    for c in clients_qs:
        clients_data.append({
            "id": str(c.id),
            "name": c.name,
            "client_code": c.client_code,
            "email": c.email,
            "phone": c.phone or "",
            "address": c.address or "",
            "status": c.status,
            "notes": c.notes or "",
            "users_count": c.users_count,
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
            "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M:%S") if c.updated_at else "",
        })

    return JsonResponse({
        "success": True,
        "total_clients": total_clients,
        "active_clients": active_clients,
        "inactive_clients": inactive_clients,
        "clients": clients_data
    })


@csrf_exempt
def api_admin_create_client(request):
    """
    POST /accounts/api/admin/clients/create/
    Creates a new Client record.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    name = (data.get("name") or "").strip()
    client_code = (data.get("client_code") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    address = (data.get("address") or "").strip()
    status = (data.get("status") or "ACTIVE").strip().upper()
    notes = (data.get("notes") or "").strip()

    if not name:
        return JsonResponse({"success": False, "error": "Client Name is required."}, status=400)

    if not client_code:
        # Auto generate code if not provided
        last_count = Client.objects.count() + 1
        client_code = f"CLT-{last_count:04d}"

    if Client.objects.filter(client_code=client_code).exists():
        return JsonResponse({"success": False, "error": f"Client code '{client_code}' already exists."}, status=400)

    if not email:
        return JsonResponse({"success": False, "error": "Client Contact Email is required."}, status=400)

    if status not in ["ACTIVE", "INACTIVE"]:
        status = "ACTIVE"

    client_obj = Client.objects.create(
        name=name,
        client_code=client_code,
        email=email,
        phone=phone,
        address=address,
        status=status,
        notes=notes
    )

    return JsonResponse({
        "success": True,
        "message": f"Client '{client_obj.name}' created successfully.",
        "client": {
            "id": str(client_obj.id),
            "name": client_obj.name,
            "client_code": client_obj.client_code,
            "email": client_obj.email,
            "phone": client_obj.phone or "",
            "address": client_obj.address or "",
            "status": client_obj.status,
            "notes": client_obj.notes or "",
            "created_at": client_obj.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
    })


@csrf_exempt
def api_admin_update_client(request, client_id):
    """
    POST /accounts/api/admin/clients/<client_id>/update/
    Updates an existing Client record or toggles status.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    if "name" in data:
        client_obj.name = data["name"].strip() or client_obj.name
    if "email" in data:
        client_obj.email = data["email"].strip().lower() or client_obj.email
    if "phone" in data:
        client_obj.phone = data["phone"].strip()
    if "address" in data:
        client_obj.address = data["address"].strip()
    if "notes" in data:
        client_obj.notes = data["notes"].strip()

    if "client_code" in data and data["client_code"].strip():
        new_code = data["client_code"].strip().upper()
        if new_code != client_obj.client_code and Client.objects.filter(client_code=new_code).exists():
            return JsonResponse({"success": False, "error": f"Client code '{new_code}' already exists."}, status=400)
        client_obj.client_code = new_code

    if "status" in data:
        st = data["status"].strip().upper()
        if st in ["ACTIVE", "INACTIVE"]:
            client_obj.status = st

    client_obj.save()

    return JsonResponse({
        "success": True,
        "message": f"Client '{client_obj.name}' updated successfully.",
        "client": {
            "id": str(client_obj.id),
            "name": client_obj.name,
            "client_code": client_obj.client_code,
            "email": client_obj.email,
            "phone": client_obj.phone or "",
            "address": client_obj.address or "",
            "status": client_obj.status,
            "notes": client_obj.notes or "",
            "updated_at": client_obj.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
    })


@csrf_exempt
def api_admin_delete_client(request, client_id):
    """
    POST /accounts/api/admin/clients/<client_id>/delete/
    Deletes a Client record.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        client_obj = Client.objects.get(id=client_id)
        name = client_obj.name
        client_obj.delete()
        return JsonResponse({"success": True, "message": f"Client '{name}' deleted successfully."})
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)


@csrf_exempt
def api_admin_stats(request):
    """
    GET /accounts/api/admin/stats/
    Returns admin overview counters.
    """
    from edi835.models import EDI835File
    total_clients = Client.objects.count()
    active_clients = Client.objects.filter(status="ACTIVE").count()
    inactive_clients = Client.objects.filter(status="INACTIVE").count()
    total_users = User.objects.count()
    total_conversions = EDI835File.objects.count()

    return JsonResponse({
        "success": True,
        "total_clients": total_clients,
        "active_clients": active_clients,
        "inactive_clients": inactive_clients,
        "total_users": total_users,
        "total_conversions": total_conversions,
        "system_status": "OPERATIONAL"
    })


# ==========================================
# ADMIN USER MANAGEMENT API ENDPOINTS
# ==========================================

@csrf_exempt
def api_admin_users(request):
    """
    GET /accounts/api/admin/users/
    Returns list of all user accounts.
    """
    search_q = request.GET.get("search", "").strip()
    users_qs = User.objects.select_related("client").all().order_by("-created_at")

    if search_q:
        users_qs = users_qs.filter(
            models.Q(name__icontains=search_q) |
            models.Q(email__icontains=search_q) |
            models.Q(mobile__icontains=search_q)
        )

    users_data = []
    for u in users_qs:
        users_data.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "mobile": u.mobile,
            "is_active": u.is_active,
            "is_staff": u.is_staff,
            "totp_enabled": u.totp_enabled,
            "client_id": str(u.client.id) if u.client else None,
            "client_name": u.client.name if u.client else None,
            "client_code": u.client.client_code if u.client else None,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
        })

    return JsonResponse({
        "success": True,
        "total_users": User.objects.count(),
        "users": users_data
    })


@csrf_exempt
def api_admin_create_user(request):
    """
    POST /accounts/api/admin/users/create/
    Creates a new user account.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or "").strip()
    password = data.get("password") or "Password@123"
    is_staff = bool(data.get("is_staff", False))
    client_id = data.get("client_id")

    if not name:
        return JsonResponse({"success": False, "error": "User Name is required."}, status=400)
    if not email:
        return JsonResponse({"success": False, "error": "User Email is required."}, status=400)
    if not mobile:
        return JsonResponse({"success": False, "error": "Mobile number is required."}, status=400)

    if User.objects.filter(email=email).exists():
        return JsonResponse({"success": False, "error": f"Email '{email}' is already registered."}, status=400)
    if User.objects.filter(mobile=mobile).exists():
        return JsonResponse({"success": False, "error": f"Mobile '{mobile}' is already registered."}, status=400)

    client_obj = None
    if client_id:
        try:
            client_obj = Client.objects.get(id=client_id)
        except Exception:
            client_obj = None

    user = User.objects.create_user(
        email=email,
        name=name,
        mobile=mobile,
        password=password,
        is_staff=is_staff,
        client=client_obj
    )

    return JsonResponse({
        "success": True,
        "message": f"User '{user.email}' created successfully.",
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "mobile": user.mobile,
            "is_staff": user.is_staff,
            "client_name": client_obj.name if client_obj else None,
        }
    })


@csrf_exempt
def api_admin_update_user(request, user_id):
    """
    POST /accounts/api/admin/users/<user_id>/update/
    Updates user details or toggles active / staff status.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        user_obj = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    if "name" in data and data["name"].strip():
        user_obj.name = data["name"].strip()
    if "mobile" in data and data["mobile"].strip():
        user_obj.mobile = data["mobile"].strip()
    if "password" in data and data["password"].strip():
        user_obj.set_password(data["password"].strip())
    if "is_active" in data:
        user_obj.is_active = bool(data["is_active"])
    if "is_staff" in data:
        user_obj.is_staff = bool(data["is_staff"])
    if "client_id" in data:
        cid = data["client_id"]
        if cid:
            try:
                user_obj.client = Client.objects.get(id=cid)
            except Exception:
                user_obj.client = None
        else:
            user_obj.client = None

    user_obj.save()

    return JsonResponse({
        "success": True,
        "message": f"User '{user_obj.email}' updated successfully."
    })


@csrf_exempt
def api_admin_delete_user(request, user_id):
    """
    POST /accounts/api/admin/users/<user_id>/delete/
    Deletes a user account.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        user_obj = User.objects.get(id=user_id)
        email = user_obj.email
        user_obj.delete()
        return JsonResponse({"success": True, "message": f"User '{email}' deleted successfully."})
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)


@csrf_exempt
def api_admin_stats(request):
    """
    GET /accounts/api/admin/stats/
    Returns admin overview counters.
    """
    total_clients = Client.objects.count()
    active_clients = Client.objects.filter(status="ACTIVE").count()
    inactive_clients = Client.objects.filter(status="INACTIVE").count()
    total_users = User.objects.count()
    total_conversions = EDI835File.objects.count()

    return JsonResponse({
        "success": True,
        "total_clients": total_clients,
        "active_clients": active_clients,
        "inactive_clients": inactive_clients,
        "total_users": total_users,
        "total_conversions": total_conversions,
        "system_status": "OPERATIONAL"
    })


# ==========================================
# ADMIN USER MANAGEMENT API ENDPOINTS
# ==========================================

@csrf_exempt
def api_admin_users(request):
    """
    GET /accounts/api/admin/users/
    Returns list of all user accounts.
    """
    search_q = request.GET.get("search", "").strip()
    users_qs = User.objects.select_related("client").all().order_by("-created_at")

    if search_q:
        users_qs = users_qs.filter(
            models.Q(name__icontains=search_q) |
            models.Q(email__icontains=search_q) |
            models.Q(mobile__icontains=search_q)
        )

    users_data = []
    for u in users_qs:
        client_name = "None"
        if u.is_superuser:
            client_name = "OneSmarter"
        elif u.is_staff:
            client_name = "OneSmarter"
        elif u.client:
            client_name = u.client.name

        users_data.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "mobile": u.mobile,
            "is_active": u.is_active,
            "is_staff": u.is_staff,
            "is_superuser": u.is_superuser,
            "role": "Super Admin" if u.is_superuser else ("Admin" if u.is_staff else "User"),
            "totp_enabled": u.totp_enabled,
            "client_id": str(u.client.id) if u.client else None,
            "client_name": client_name,
            "client_code": u.client.client_code if u.client else None,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
            "clients": [client_name],
            "person": u.name,
            "mfa": "Enabled" if u.totp_enabled else "Disabled",
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "status": "Active" if u.is_active else "Inactive",
        })

    return JsonResponse({
        "success": True,
        "total_users": User.objects.count(),
        "users": users_data
    })


@csrf_exempt
def api_admin_create_user(request):
    """
    POST /accounts/api/admin/users/create/
    Creates a new user account.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or "").strip()
    password = data.get("password") or "Password@123"
    
    role = data.get("role", "User")
    is_staff = bool(data.get("is_staff", False) or role in ["Admin", "Super Admin"])
    is_superuser = bool(data.get("is_superuser", False) or role == "Super Admin")
    client_id = data.get("client_id")

    if not name:
        return JsonResponse({"success": False, "error": "User Name is required."}, status=400)
    if not email:
        return JsonResponse({"success": False, "error": "User Email is required."}, status=400)
    if not mobile:
        return JsonResponse({"success": False, "error": "Mobile number is required."}, status=400)

    if User.objects.filter(email=email).exists():
        return JsonResponse({"success": False, "error": f"Email '{email}' is already registered."}, status=400)
    if User.objects.filter(mobile=mobile).exists():
        return JsonResponse({"success": False, "error": f"Mobile '{mobile}' is already registered."}, status=400)

    # Permission check: Only superusers can create staff or superusers
    if (is_staff or is_superuser) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Access Denied: Only Super Admins can create Admin or Super Admin accounts."}, status=403)

    client_obj = None
    if not is_staff:
        if client_id:
            try:
                client_obj = Client.objects.get(id=client_id)
            except Exception:
                client_obj = None
        if not client_obj:
            return JsonResponse({"success": False, "error": "Client assignment is required for standard Users."}, status=400)

    user = User.objects.create_user(
        email=email,
        name=name,
        mobile=mobile,
        password=password,
        is_staff=is_staff,
        is_superuser=is_superuser,
        client=client_obj
    )

    return JsonResponse({
        "success": True,
        "message": f"User '{user.email}' created successfully.",
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "mobile": user.mobile,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "role": "Super Admin" if user.is_superuser else ("Admin" if user.is_staff else "User"),
            "client_name": client_obj.name if client_obj else ("OneSmarter" if is_staff else None),
        }
    })


@csrf_exempt
def api_admin_update_user(request, user_id):
    """
    POST /accounts/api/admin/users/<user_id>/update/
    Updates user details or toggles active / staff status.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        user_obj = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    # Permission check: Only Super Admins can edit Admins or Super Admins
    if (user_obj.is_staff or user_obj.is_superuser) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Access Denied: Only Super Admins can update Admin or Super Admin accounts."}, status=403)

    role = data.get("role")
    new_is_staff = user_obj.is_staff
    new_is_superuser = user_obj.is_superuser

    if "is_staff" in data or role:
        new_is_staff = bool(data.get("is_staff", False) or role in ["Admin", "Super Admin"])
    if "is_superuser" in data or role:
        new_is_superuser = bool(data.get("is_superuser", False) or role == "Super Admin")

    # If role settings changed, requester must be superuser
    if (new_is_staff != user_obj.is_staff or new_is_superuser != user_obj.is_superuser) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Access Denied: Only Super Admins can change administrative roles."}, status=403)

    if "email" in data and data["email"].strip().lower():
        email = data["email"].strip().lower()
        if email != user_obj.email:
            if User.objects.filter(email=email).exists():
                return JsonResponse({"success": False, "error": f"Email '{email}' is already registered in the system."}, status=400)
            user_obj.email = email

    if "name" in data and data["name"].strip():
        user_obj.name = data["name"].strip()
    if "mobile" in data and data["mobile"].strip():
        mobile = data["mobile"].strip()
        if mobile != user_obj.mobile:
            if User.objects.filter(mobile=mobile).exists():
                return JsonResponse({"success": False, "error": f"Mobile '{mobile}' is already registered in the system."}, status=400)
            user_obj.mobile = mobile
    if "password" in data and data["password"].strip():
        user_obj.set_password(data["password"].strip())
    if "is_active" in data:
        user_obj.is_active = bool(data["is_active"])
    
    user_obj.is_staff = new_is_staff
    user_obj.is_superuser = new_is_superuser

    if user_obj.is_staff:
        user_obj.client = None
    else:
        if "client_id" in data:
            cid = data["client_id"]
            if not cid:
                return JsonResponse({"success": False, "error": "Client assignment is required for standard Users."}, status=400)
            try:
                user_obj.client = Client.objects.get(id=cid)
            except Exception:
                return JsonResponse({"success": False, "error": "Invalid client assigned."}, status=400)
        elif not user_obj.client:
            return JsonResponse({"success": False, "error": "Client assignment is required for standard Users."}, status=400)

    user_obj.save()

    return JsonResponse({
        "success": True,
        "message": f"User '{user_obj.email}' updated successfully."
    })


@csrf_exempt
def api_admin_delete_user(request, user_id):
    """
    POST /accounts/api/admin/users/<user_id>/delete/
    Deletes a user account.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        user_obj = User.objects.get(id=user_id)
        # Permission check: Only Super Admins can delete Admins or Super Admins
        if (user_obj.is_staff or user_obj.is_superuser) and not request.user.is_superuser:
            return JsonResponse({"success": False, "error": "Access Denied: Only Super Admins can delete Admin or Super Admin accounts."}, status=403)
        email = user_obj.email
        user_obj.delete()
        return JsonResponse({"success": True, "message": f"User '{email}' deleted successfully."})
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)


@csrf_exempt
def api_client_contacts(request):
    """ GET /accounts/api/contacts/ """
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Not authenticated"}, status=401)
    
    if not request.user.client:
        return JsonResponse({"success": False, "error": "User has no associated client"}, status=400)
        
    try:
        from accounts.models import ClientContact
        contacts = ClientContact.objects.filter(client=request.user.client).order_by('-created_at').values(
            "id", "role_name", "name", "email", "phone", "created_at"
        )
        return JsonResponse({"success": True, "contacts": list(contacts)})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@csrf_exempt
def api_change_password(request):
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Authentication required."}, status=401)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed."}, status=405)
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    new_password = data.get("password", "").strip()
    if not new_password:
        return JsonResponse({"success": False, "error": "Password cannot be empty."}, status=400)

    user = request.user
    user.set_password(new_password)
    user.first_login = False
    user.save()
    
    from django.contrib.auth import update_session_auth_hash
    update_session_auth_hash(request, user)

    return JsonResponse({"success": True, "message": "Password changed successfully."})


@csrf_exempt
def api_admin_reset_password(request, user_id):
    """
    POST /accounts/api/admin/users/<user_id>/reset-password/
    Resets the user's password, generates a temporary password, sets first_login to True,
    and sends an email with the temporary password.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
        
    try:
        user_obj = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)
        
    # Generate temporary password
    import random
    import string
    chars = string.ascii_letters + string.digits
    temp_pass = "Temp@" + "".join(random.choice(chars) for _ in range(8))
    
    user_obj.set_password(temp_pass)
    user_obj.first_login = True
    user_obj.save()
    
    # Send email
    from admin_panel.email_service import send_client_email
    subject = "OneSmarter Inc: Account Password Reset"
    html = f"""
    <p>Dear {user_obj.name or 'User'},</p>
    <p>Your password for the OneSmarter portal has been reset by the administrator.</p>
    <p>Your temporary password is: <b>{temp_pass}</b></p>
    <p>Please note that you will be required to change this password upon your next login.</p>
    <p>Sincerely,<br/>OneSmarter Inc, USA</p>
    """
    
    success = False
    if user_obj.client:
        success = send_client_email(user_obj.client, subject, html, to_emails=[user_obj.email])
    else:
        # Fall back to default SMTP
        from admin_panel.models import ClientSmtpConfig
        default_config = ClientSmtpConfig.objects.filter(client__isnull=True).first()
        if default_config:
            try:
                from admin_panel.email_service import get_client_email_backend
                backend = get_client_email_backend(default_config)
                from_email = f"{default_config.sender_name} <{default_config.sender_email}>" if default_config.sender_name else default_config.sender_email
                from django.core.mail import EmailMultiAlternatives
                from django.utils.html import strip_tags
                text_content = strip_tags(html)
                msg = EmailMultiAlternatives(subject, text_content, from_email, [user_obj.email], connection=backend)
                msg.attach_alternative(html, "text/html")
                msg.send()
                success = True
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to send default reset email: {e}")
                
    return JsonResponse({
        "success": True,
        "message": f"Password reset successfully for {user_obj.email}. Email status: {'Sent' if success else 'Failed to send'}"
    })
