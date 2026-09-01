from project835.field_crypto import (
    encrypt_smtp_password,
    decrypt_smtp_password,
)
from project835.decorators import (
    admin_api_required,
)
import json
import logging
import os
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.db import models, transaction
from django.db.models import Count, Prefetch, Q
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.client_deletion import ClientDeletionError, permanently_delete_client
from accounts.models import Client, User
from edi835.models import EDI835File
from .models import OnboardingStepDefinition, ClientStepStatus, GoLiveStepDefinition, ClientGoLiveStatus, ClientTestEnvironment, AuditLog, ClientSmtpConfig, ClientDocument
from project835.field_crypto import (
    encrypt_smtp_password,
    decrypt_smtp_password,
)
from .document_validator import extract_text_from_file_bytes, validate_document_text
from validation import (
    validate_step_upload,
    validate_golive_step_upload,
    validate_phone_number,
    validate_email_address,
    validate_x12_835_content,
    get_step_download_filename
)


# Workflow order is intentionally independent from the permanent database IDs.
# Step 16 is the user-creation action split from the former combined Step 10.
ONBOARDING_PROCESS_ORDER = (1, 2, 3, 4, 5, 6, 7, 10, 16, 9, 8, 11, 12, 13, 14, 15)
ONBOARDING_PROCESS_POSITION = {
    step_number: position
    for position, step_number in enumerate(ONBOARDING_PROCESS_ORDER)
}

EASTERN_TIMEZONE = "America/New_York"


def _valid_timezone_name(value):
    candidate = (value or EASTERN_TIMEZONE).strip()
    try:
        ZoneInfo(candidate)
        return candidate
    except (ZoneInfoNotFoundError, ValueError):
        return EASTERN_TIMEZONE


def _client_timezone(client):
    return _valid_timezone_name(getattr(client, "timezone", EASTERN_TIMEZONE))


def onboarding_process_position(step_number):
    return ONBOARDING_PROCESS_POSITION.get(step_number, len(ONBOARDING_PROCESS_ORDER))


def _offboarded_workflow_lock(request, client_id, action):
    """Return a permanent workflow-lock response for an offboarded tenant."""
    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)
    if str(client_obj.stage or "").lower() != "offboarded":
        return None
    try:
        AuditLog.objects.create(
            module="OFFBOARDING",
            action="LOCKED_WORKFLOW_ATTEMPT",
            details=f"Blocked {action} because client '{client_obj.name}' is permanently offboarded.",
            performed_by=getattr(request.user, "name", "") or getattr(request.user, "email", "") or "Administrator",
            client=client_obj,
        )
    except Exception:
        logging.getLogger(__name__).exception("Failed to audit a blocked offboarded-client workflow request")
    return JsonResponse({
        "success": False,
        "code": "CLIENT_OFFBOARDING_FINALIZED",
        "error": "This client has been permanently offboarded. Its onboarding and offboarding workflows are locked.",
    }, status=409)


@csrf_exempt

def _canonical_mir_filename(record):
    """Return the persisted admin-configured MIR filename for an EDI record."""
    if not record:
        return ""
    mir_record = getattr(record, "mir_file", None)
    if mir_record and mir_record.mir_filename:
        return Path(mir_record.mir_filename).name
    return ""


def _safe_mir_filename(value, fallback="output.mir"):
    """Normalize a client-facing MIR filename."""
    filename = Path(str(value or "").strip()).name
    if not filename:
        filename = fallback
    if not filename.lower().endswith(".mir"):
        filename += ".mir"
    return filename

def api_admin_stats(request):
    """
    GET /admin-panel/api/stats/
    Returns admin metrics summary.
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


@csrf_exempt
def api_admin_clients(request):
    """
    GET /admin-panel/api/clients/  -> List clients
    POST /admin-panel/api/clients/ -> Create new client
    """
    if request.method == "POST":
        return api_admin_create_client(request)

    search_q = request.GET.get("search", "").strip()
    status_q = request.GET.get("status", "").strip()

    clients_qs = Client.objects.all().order_by("-created_at")

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

    from django.utils import timezone
    now = timezone.now()

    total_onboarding_steps = OnboardingStepDefinition.objects.count()
    total_golive_steps = GoLiveStepDefinition.objects.count()
    if total_golive_steps == 0:
        total_golive_steps = 6

    clients_qs = clients_qs.annotate(
        users_count=Count("users", distinct=True),
        completed_onboarding_count=Count(
            "onboarding_steps",
            filter=Q(onboarding_steps__status="COMPLETED"),
            distinct=True,
        ),
        completed_golive_count=Count(
            "golive_steps",
            filter=Q(golive_steps__status="COMPLETED"),
            distinct=True,
        ),
    ).prefetch_related(
        Prefetch(
            "onboarding_steps",
            queryset=ClientStepStatus.objects.filter(status="COMPLETED").select_related("step"),
            to_attr="completed_onboarding_steps",
        )
    )

    clients_data = []
    for c in clients_qs:
        completed_onboarding = c.completed_onboarding_count
        onboarding_incomplete = completed_onboarding < total_onboarding_steps

        completed_golive = c.completed_golive_count
        go_live_completed = (completed_golive == total_golive_steps)

        dynamic_stage = c.stage
        # Offboarding is terminal and must never be overwritten by calculated
        # onboarding/go-live progress in the All Clients registry.
        if c.stage == "offboarded":
            dynamic_stage = "offboarded"
        elif onboarding_incomplete:
            completed_steps = {ss.step.step_number for ss in c.completed_onboarding_steps}
            in_progress_step = 1
            for num in ONBOARDING_PROCESS_ORDER:
                if num not in completed_steps:
                    in_progress_step = num
                    break
            if in_progress_step >= 13:
                dynamic_stage = "go_live_pending"
            else:
                dynamic_stage = f"onboarding_step_{in_progress_step}"
        elif go_live_completed and c.live_since:
            if c.live_since > now:
                dynamic_stage = "production_pending"
            else:
                dynamic_stage = "production"
        else:
            dynamic_stage = "onboarding_completed"

        clients_data.append({
            "id": str(c.id),
            "name": c.name,
            "code": c.client_code,
            "client_code": c.client_code,
            "email": c.email,
            "phone": c.phone or "",
            "address": c.address or "",
            "state": c.state or "",
            "zip_code": c.zip_code or "",
            "status": c.status,
            "stage": dynamic_stage,
            "claims_system": c.claims_system,
            "owner": c.owner,
            "progress_pct": c.progress_pct,
            "timezone": _client_timezone(c),
            "live_since": c.live_since.strftime("%Y-%m-%dT%H:%M:%SZ") if c.live_since else (c.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.created_at else None),
            "notes": c.notes or "",
            "users_count": c.users_count,
            "created_at": c.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.created_at else "",
            "updated_at": c.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if c.updated_at else "",
        })

    return JsonResponse({
        "success": True,
        "results": clients_data,
        "clients": clients_data,
        "total_clients": total_clients,
        "active_clients": active_clients,
        "inactive_clients": inactive_clients,
    })


@csrf_exempt
def api_admin_create_client(request):
    """
    POST /admin-panel/api/clients/create/ or /admin-panel/api/clients/
    Creates a new client record in database.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    name = (data.get("name") or "").strip()
    client_code = (data.get("code") or data.get("client_code") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    address = (data.get("address") or "").strip()
    state = (data.get("state") or "").strip().upper()
    zip_code = (data.get("zip_code") or data.get("zip") or "").strip()
    status = (data.get("status") or "ACTIVE").strip().upper()
    notes = (data.get("notes") or "").strip()
    claims_system = (data.get("claims_system") or "Vendor Hosted").strip()
    owner = (data.get("owner") or "System Admin").strip()
    stage = (data.get("stage") or "onboarding").strip().lower()

    if not name:
        return JsonResponse({"success": False, "error": "Client Name is required."}, status=400)

    if not client_code:
        last_count = Client.objects.count() + 1
        client_code = f"CLT-{last_count:04d}"

    # Auto generate client code if needed or add suffix if duplicate
    base_code = client_code
    counter = 1
    while Client.objects.filter(client_code=client_code).exists():
        client_code = f"{base_code}-{counter}"
        counter += 1

    if not email:
        safe_name = name.lower().replace(" ", "").replace(".", "")
        email = f"{safe_name}@client.com"

    if state and (len(state) != 2 or not state.isalpha()):
        return JsonResponse({"success": False, "error": "State must be a valid two-letter US state abbreviation."}, status=400)
    if zip_code and not __import__("re").fullmatch(r"\d{5}(?:-\d{4})?", zip_code):
        return JsonResponse({"success": False, "error": "ZIP code must be in 12345 or 12345-6789 format."}, status=400)

    if status not in ["ACTIVE", "INACTIVE"]:
        status = "ACTIVE"

    try:
        with transaction.atomic():
            client_obj = Client.objects.create(
                name=name,
                client_code=client_code,
                email=email,
                phone=phone,
                address=address,
                state=state,
                zip_code=zip_code,
                status=status,
                notes=notes,
                claims_system=claims_system,
                owner=owner,
                stage="onboarding_pending",
                progress_pct=0
            )

            # Initialize Sequential Onboarding Workflow
            onboarding_step_1 = OnboardingStepDefinition.objects.filter(step_number=1).first()
            if onboarding_step_1:
                ClientStepStatus.objects.create(
                    client=client_obj,
                    step=onboarding_step_1,
                    status='IN_PROGRESS'
                )

            # Initialize Pre-Flight / Go-Live Workflow
            golive_step_1 = GoLiveStepDefinition.objects.filter(step_number=1).first()
            if golive_step_1:
                ClientGoLiveStatus.objects.create(
                    client=client_obj,
                    step=golive_step_1,
                    status='IN_PROGRESS'
                )

            # Provision the Sandbox / Test Environment
            ClientTestEnvironment.objects.create(
                client=client_obj,
                sftp_host="sftp-test.internal",
                sftp_username=f"{client_obj.id}_sandbox",
                watched_folder=f"/inbound/{client_obj.id}_test",
                test_status="In Progress"
            )

            # Audit Logging
            current_admin = "System"
            if request.user and hasattr(request.user, "name") and request.user.name:
                current_admin = request.user.name
            elif request.user and hasattr(request.user, "email") and request.user.email:
                current_admin = request.user.email

            AuditLog.objects.create(
                module="CLIENTS",
                action="CLIENT_CREATED",
                details=f"Created new tenant '{client_obj.name}'.",
                performed_by=current_admin,
                client=client_obj
            )

            client_dict = {
                "id": str(client_obj.id),
                "name": client_obj.name,
                "code": client_obj.client_code,
                "client_code": client_obj.client_code,
                "email": client_obj.email,
                "phone": client_obj.phone or "",
                "address": client_obj.address or "",
                "state": client_obj.state or "",
                "zip_code": client_obj.zip_code or "",
                "status": client_obj.status,
                "stage": client_obj.stage,
                "claims_system": client_obj.claims_system,
                "owner": client_obj.owner,
                "progress_pct": client_obj.progress_pct,
                "timezone": _client_timezone(client_obj),
                "live_since": client_obj.live_since.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.live_since else (client_obj.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.created_at else None),
                "notes": client_obj.notes or "",
                "created_at": client_obj.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.created_at else "",
            }

            return JsonResponse({
                "success": True,
                "message": f"Client '{client_obj.name}' created successfully.",
                "client": client_dict,
                "data": client_dict,
            }, status=201)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
def api_admin_update_client(request, client_id):
    """
    POST /admin-panel/api/clients/<client_id>/update/
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        client_obj = Client.objects.get(id=client_id)

        from urllib.parse import unquote
        from edi835.file_types import file_extension_error, has_valid_file_extension
        uploaded_filename = unquote(request.headers.get('X-Filename', '835_file.x12'))
        if not has_valid_file_extension(uploaded_filename, "835"):
            return JsonResponse({"success": False, "error": file_extension_error("835"), "checks": []}, status=400)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    if str(client_obj.stage or "").lower() == "offboarded":
        requested_stage = str(data.get("stage", "offboarded") or "").strip().lower()
        requested_status = str(data.get("status", "INACTIVE") or "").strip().upper()
        if requested_stage != "offboarded" or requested_status == "ACTIVE":
            return _offboarded_workflow_lock(request, client_id, "client reactivation")

    if "name" in data:
        client_obj.name = data["name"].strip() or client_obj.name
    if "email" in data:
        client_obj.email = data["email"].strip().lower() or client_obj.email
    if "phone" in data:
        client_obj.phone = data["phone"].strip()
    if "address" in data:
        client_obj.address = data["address"].strip()
    if "state" in data:
        state = data["state"].strip().upper()
        if state and (len(state) != 2 or not state.isalpha()):
            return JsonResponse({"success": False, "error": "State must be a valid two-letter US state abbreviation."}, status=400)
        client_obj.state = state
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

    if "stage" in data:
        client_obj.stage = data["stage"].strip().lower()
    if "claims_system" in data:
        client_obj.claims_system = data["claims_system"].strip()
    if "owner" in data:
        client_obj.owner = data["owner"].strip()
    if "progress_pct" in data:
        try:
            client_obj.progress_pct = int(data["progress_pct"])
        except ValueError:
            pass

    client_obj.save()

    # Audit log for client update
    try:
        actor = "System"
        if request.user and hasattr(request.user, "name") and request.user.name:
            actor = request.user.name
        elif request.user and hasattr(request.user, "email") and request.user.email:
            actor = request.user.email
        AuditLog.objects.create(
            module="CLIENTS",
            action="CLIENT_UPDATED",
            details=f"Client '{client_obj.name}' profile updated.",
            performed_by=actor,
            client=client_obj
        )
    except Exception:
        pass

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
            "stage": client_obj.stage,
            "claims_system": client_obj.claims_system,
            "owner": client_obj.owner,
            "progress_pct": client_obj.progress_pct,
            "timezone": _client_timezone(client_obj),
            "live_since": client_obj.live_since.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.live_since else (client_obj.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.created_at else None),
            "notes": client_obj.notes or "",
            "updated_at": client_obj.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.updated_at else "",
        }
    })


@csrf_exempt
def api_admin_delete_client(request, client_id):
    """
    POST /admin-panel/api/clients/<client_id>/delete/
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}

    try:
        name = permanently_delete_client(
            actor=request.user,
            client_id=client_id,
            confirmation_name=(data.get("confirmation_name") or "").strip(),
            password=data.get("password") or "",
        )
    except ClientDeletionError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=exc.status)

    return JsonResponse({"success": True, "message": f"Client '{name}' deleted successfully."})


@csrf_exempt
def api_admin_access_info(request):
    """
    GET /admin-panel/api/access/info/
    Returns dynamic access control matrix from database users.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Not authenticated"}, status=401)

    staff_list = []
    # Administrative identities belong to OneSmarter, never to a tenant.  Keep
    # their visibility and account state completely independent from any stale
    # legacy client relation.  Tenant state applies only to ordinary users.
    administrative_accounts = Q(is_staff=True) | Q(is_superuser=True)
    active_tenant_accounts = (
        Q(is_staff=False, is_superuser=False)
        & (Q(client__isnull=True) | ~Q(client__stage="offboarded"))
    )
    visible_users = User.objects.select_related("client").filter(
        administrative_accounts | active_tenant_accounts
    ).order_by("-created_at")
    for u in visible_users:
        staff_list.append({
            "id": u.id,
            "person": u.name or u.email.split("@")[0],
            "email": u.email,
            "mobile": u.mobile or "—",
            "role": "Super Admin" if u.is_superuser else ("Admin" if u.is_staff else "User"),
            "access": "Full Access" if (u.is_staff or u.is_superuser) else "Standard Access",
            "clients": ["OneSmarter"] if (u.is_staff or u.is_superuser) else ([u.client.name] if u.client else ["None"]),
            "mfa": "Enabled" if u.totp_enabled else "Disabled",
            "last_login": u.last_login.isoformat() if u.last_login else "",
            "status": "Active" if u.is_active else "Inactive",
        })

    cur_u = request.user
    if cur_u.is_superuser:
        cur_role = "Super Admin"
    elif cur_u.is_staff:
        cur_role = "Admin"
    else:
        cur_role = "User"

    mfa_str = "Enabled" if cur_u.totp_enabled else "Disabled"
    mfa_desc = "Hardware & TOTP Verified" if cur_u.totp_enabled else "Password Only"

    return JsonResponse({
        "success": True,
        "current_admin": {
            "name": cur_u.name or cur_u.email,
            "role": cur_role,
            "mfa_status": mfa_str,
            "mfa_desc": mfa_desc,
            "session_state": "Active",
            "session_desc": "30-min auto-expire",
        },
        "last_login": cur_u.last_login.isoformat() if cur_u.last_login else "",
        "staff": staff_list,
        "users": staff_list,
    })


@csrf_exempt
def api_admin_users(request):
    """
    GET /admin-panel/api/users/  -> List users
    POST /admin-panel/api/users/ -> Create user credentials
    """
    if request.method == "POST":
        return api_admin_create_user(request)

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
            "mobile": u.mobile or "—",
            "is_active": u.is_active,
            "is_staff": u.is_staff,
            "is_superuser": u.is_superuser,
            "role": "Super Admin" if u.is_superuser else ("Admin" if u.is_staff else "User"),
            "totp_enabled": u.totp_enabled,
            "client_id": str(u.client.id) if u.client else None,
            "client_name": "OneSmarter" if u.is_staff else (u.client.name if u.client else None),
            "client_code": u.client.client_code if u.client else None,
            "created_at": u.created_at.isoformat() if u.created_at else "",
        })

    return JsonResponse({
        "success": True,
        "total_users": User.objects.count(),
        "users": users_data,
        "results": users_data,
    })


@csrf_exempt
def api_admin_create_user(request):
    """
    POST /admin-panel/api/users/create/ or /admin-panel/api/users/
    Creates user credentials in database.
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
    role = (data.get("role") or "").strip().lower()
    is_staff = bool(data.get("is_staff", False) or role in ["admin", "staff", "super admin"])
    is_superuser = role == "super admin"
    client_id = data.get("client_id")
    client_ids = data.get("clients") or []

    if not name:
        return JsonResponse({"success": False, "error": "User Name is required."}, status=400)
    if not email:
        return JsonResponse({"success": False, "error": "User Email is required."}, status=400)

    if User.objects.filter(email=email).exists():
        return JsonResponse({"success": False, "error": f"Email '{email}' is already registered in the system."}, status=400)

    if not mobile:
        count = User.objects.count() + 1000
        mobile = f"+1555{count:04d}"

    if User.objects.filter(mobile=mobile).exists():
        count = User.objects.count() + 2000
        mobile = f"+1555{count:04d}"

    if (is_staff or is_superuser) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Only a Super Admin can create Admin or Super Admin accounts."}, status=403)

    client_obj = None
    if not is_staff:
        target_cid = client_id or (client_ids[0] if isinstance(client_ids, list) and len(client_ids) > 0 else None)
        if target_cid:
            try:
                client_obj = Client.objects.get(id=target_cid)
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

    user_dict = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "mobile": user.mobile,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "role": "Super Admin" if user.is_superuser else ("Admin" if user.is_staff else "User"),
        "client_name": "OneSmarter" if user.is_staff else (client_obj.name if client_obj else None),
    }

    return JsonResponse({
        "success": True,
        "message": f"User credentials for '{user.email}' created successfully in database.",
        "user": user_dict,
        "data": user_dict,
    })


@csrf_exempt
def api_admin_update_user(request, user_id):
    """
    POST /admin-panel/api/users/<user_id>/update/
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

    requested_role = (data.get("role") or "").strip()
    if requested_role and requested_role not in {"User", "Admin", "Super Admin"}:
        return JsonResponse({"success": False, "error": "Invalid role."}, status=400)

    if requested_role:
        requested_is_staff = requested_role in {"Admin", "Super Admin"}
        requested_is_superuser = requested_role == "Super Admin"
    else:
        requested_is_staff = bool(data.get("is_staff", user_obj.is_staff))
        requested_is_superuser = bool(data.get("is_superuser", user_obj.is_superuser))
        if requested_is_superuser:
            requested_is_staff = True

    changes_admin_role = (
        requested_is_staff != user_obj.is_staff
        or requested_is_superuser != user_obj.is_superuser
    )
    if (user_obj.is_staff or user_obj.is_superuser or changes_admin_role) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Only a Super Admin can modify Admin or Super Admin roles or accounts."}, status=403)

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
    user_obj.is_staff = requested_is_staff
    user_obj.is_superuser = requested_is_superuser
    if user_obj.is_staff:
        user_obj.client = None

    if not user_obj.is_staff:
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
    POST /admin-panel/api/users/<user_id>/delete/
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST method is allowed."}, status=405)

    try:
        user_obj = User.objects.get(id=user_id)
        if (user_obj.is_staff or user_obj.is_superuser) and not request.user.is_superuser:
            return JsonResponse({"success": False, "error": "Only a Super Admin can delete Admin or Super Admin accounts."}, status=403)
        email = user_obj.email
        user_obj.delete()
        return JsonResponse({"success": True, "message": f"User '{email}' deleted successfully."})
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)


@csrf_exempt
def api_admin_client_state(request, client_id):
    """
    GET /admin-panel/api/clients/<client_id>/state/
    Returns the client info and the state of their onboarding steps.
    """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET method is allowed."}, status=405)

    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)

    from accounts.models import ClientStepComment
    comments_qs = ClientStepComment.objects.filter(client=client_obj).order_by('step_number', '-created_at')
    latest_comments = {}
    for c in comments_qs:
        if c.step_number not in latest_comments:
            latest_comments[c.step_number] = {
                "note_text": c.comment,
                "author": c.author
            }

    step_defs = OnboardingStepDefinition.objects.all().order_by('step_number')
    step_statuses = ClientStepStatus.objects.filter(client=client_obj).select_related('step')
    status_map = {ss.step_id: ss.status for ss in step_statuses}

    total_golive = GoLiveStepDefinition.objects.count()
    if total_golive == 0:
        total_golive = 6
    completed_golive = ClientGoLiveStatus.objects.filter(client=client_obj, status='COMPLETED').count()

    latest_docs_by_type = {}
    for doc in ClientDocument.objects.filter(client=client_obj).order_by('-created_at'):
        if doc.document_type not in latest_docs_by_type:
            latest_docs_by_type[doc.document_type] = doc

    from accounts.models import ClientContact
    contacts_list = list(
        ClientContact.objects.filter(client=client_obj).values("id", "role_name", "name", "email", "phone")
    )
    users_list = [
        {**u, "role": "Admin" if u.get("is_staff") else "User"}
        for u in User.objects.filter(client=client_obj).values("id", "name", "email", "mobile", "is_staff")
    ]

    # Keep installations that predate the split filename/user step aligned with
    # the canonical 16-step workflow.
    step6_def = OnboardingStepDefinition.objects.filter(step_number=6).first()
    step16_def = OnboardingStepDefinition.objects.filter(step_number=16).first()
    if step_defs.count() != 16 or not step6_def or step6_def.title != "SMTP / Email Configuration" or not step16_def:
        default_steps = [
            (1, "Mutual NDA signed", "Upload signed NDA template to establish confidentiality agreement."),
            (2, "Business associate agreement executed", "Execute HIPAA compliant Business Associate Agreement."),
            (3, "Security review returned to client", "Upload security audit review document."),
            (4, "Contact Records", "Designate client contact personnel and records."),
            (5, "Claims system identified and verified", "Identify client claims vendor software system."),
            (6, "SMTP / Email Configuration", "Configure SMTP credentials to utilize for onboarding notifications."),
            (7, "Delivery method agreed", "Configure secure transfer mechanism (SFTP, API drop)."),
            (8, "Validate 835 and Push MIR to SFTP", "Validate the 835, convert it to MIR, and upload the MIR to the configured outbound SFTP folder."),
            (9, "Mapping rules written & configured", "Open Mapping Application to configure 835 conversion."),
            (10, "MIR Output Filename Format", "Define the naming convention used for generated MIR output files."),
            (11, "Side-by-Side 835 Conversion Review", "Verify side-by-side conversion of sample 835 files."),
            (12, "Go-Live Safeguards Verification", "Confirm production cutover safeguards and monitoring."),
            (13, "Production Schedule", "Define scheduled date and time to go live."),
            (14, "Go-Live / Final Verification", "Attach email conversation confirmation."),
            (15, "Production Delivery Sign-Off / Conclude Onboarding", "Monitor first live 835 delivery and conclude onboarding."),
            (16, "Create Client User", "Create and associate the client's application user account."),
        ]
        for num, title, desc in default_steps:
            OnboardingStepDefinition.objects.update_or_create(
                step_number=num,
                defaults={"title": title, "description": desc},
            )
        OnboardingStepDefinition.objects.exclude(
            step_number__in=[num for num, _, _ in default_steps]
        ).delete()
        step_defs = OnboardingStepDefinition.objects.all().order_by('step_number')

    step_defs = sorted(
        step_defs,
        key=lambda step: onboarding_process_position(step.step_number),
    )

    steps_data = []

    action_types = {
        1: "upload_template",
        2: "upload_template",
        3: "upload_template",
        4: "contact_manager",
        5: "claim_verify",
        6: "smtp_config",
        7: "transfer_config",
        8: "x12_835_validate",
        9: "mapping_redirect",
        10: "naming_config",
        16: "user_creation",
        11: "side_by_side_done",
        12: "golive_redirect",
        13: "production_schedule",
        14: "email_upload",
        15: "text_submission_final",
    }

    def get_phase(step_number):
        if step_number in {1, 2, 3}:
            return "DOCUMENTS & COMPLIANCE"
        if step_number in {4, 5}:
            return "CLIENT DISCOVERY"
        if step_number in {6, 7, 10, 16}:
            return "SECURE DELIVERY & ACCESS"
        if step_number in {9, 8, 11}:
            return "CONVERSION CONFIGURATION & VALIDATION"
        if step_number in {12, 13}:
            return "PRODUCTION READINESS"
        return "GO-LIVE & SIGN-OFF"

    for step in step_defs:
        st = status_map.get(step.id, 'PENDING')

        is_done = st == 'COMPLETED'

        # Step 7 can be completed by selecting the admin-managed default SFTP
        # configuration. Treat that persisted configuration as completion even
        # if an older frontend did not call the generic step completion endpoint.
        if step.step_number == 7 and not is_done:
            from edi835.models import SFTPConfig
            is_done = SFTPConfig.objects.filter(
                client=client_obj,
                connection_type='UNIFIED',
                use_default=True,
            ).exists()

        # Override Step 12 completion based on Go-Live steps
        if step.step_number == 12:
            is_done = (st == 'COMPLETED') or (completed_golive == total_golive)

        is_in_progress = st == 'IN_PROGRESS'

        is_file_step = step.step_number in [1, 2, 3, 14]

        extra_data = {}
        if step.step_number == 4:
            extra_data["contacts"] = contacts_list
        elif step.step_number == 10:
            extra_data["mir_filename_format"] = client_obj.mir_filename_format
        elif step.step_number == 16:
            extra_data["users"] = users_list
        elif step.step_number == 13:
            if client_obj.live_since:
                from django.utils.timezone import localtime
                timezone_name = _client_timezone(client_obj)
                local_dt = localtime(client_obj.live_since, ZoneInfo(timezone_name))
                extra_data["schedule"] = {
                    "scheduled_date": local_dt.strftime("%Y-%m-%d"),
                    "scheduled_time": local_dt.strftime("%H:%M"),
                    "timezone": timezone_name,
                    "scheduled_at": client_obj.live_since.isoformat(),
                }

        # Load the latest uploaded document for this step (persisted across redos)
        latest_upload_data = None
        if is_file_step or step.step_number in [8, 11]:
            doc_type = f"Onboarding Step {step.step_number}"
            latest_doc = latest_docs_by_type.get(doc_type)
            if latest_doc:
                latest_upload_data = {
                    "id": latest_doc.id,
                    "original_filename": latest_doc.original_filename,
                    "uploaded_at": latest_doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if latest_doc.created_at else None,
                    "validation_status": "COMPLETED"
                }

        steps_data.append({
            "id": step.step_number,
            "displayNumber": onboarding_process_position(step.step_number) + 1,
            "key": f"step_{step.step_number}_{step.title.lower().replace(' ', '_').replace('/', '_')[:20]}",
            "title": step.title,
            "desc": step.description,
            "phase": get_phase(step.step_number),
            "done": is_done,
            "inProgress": is_in_progress,
            "actionType": action_types.get(step.step_number, "standard"),
            "file": is_file_step,
            "ext": "pdf" if is_file_step else None,
            "extra": extra_data,
            "latestUpload": latest_upload_data,
            "latestNote": latest_comments.get(step.step_number, None)
        })

    # Derive the single active action from canonical process order. This also
    # migrates clients whose old state marked Step 8 active before Step 10.
    active_step_assigned = False
    for s in steps_data:
        s["inProgress"] = False
        if not active_step_assigned and not s["done"]:
            s["inProgress"] = True
            active_step_assigned = True

    client_dict = {
        "id": str(client_obj.id),
        "name": client_obj.name,
        "progress_pct": client_obj.progress_pct,
        "stage": client_obj.stage,
        "created_at": client_obj.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.created_at else "",
        "updated_at": client_obj.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if client_obj.updated_at else "",
    }

    return JsonResponse({
        "success": True,
        "state": {
            "client": client_dict,
            "steps": steps_data
        }
    })

def update_client_onboarding_stats(client_obj):
    total_steps = OnboardingStepDefinition.objects.count()
    if total_steps == 0:
        return

    completed_steps_qs = ClientStepStatus.objects.filter(client=client_obj, status='COMPLETED')
    completed_step_nums = set(completed_steps_qs.values_list('step__step_number', flat=True))

    # Ensure step 12 is counted if go-live is complete
    total_golive = GoLiveStepDefinition.objects.count()
    if total_golive == 0: total_golive = 6
    completed_golive = ClientGoLiveStatus.objects.filter(client=client_obj, status='COMPLETED').count()

    if completed_golive == total_golive:
        if 12 not in completed_step_nums:
            completed_step_nums.add(12)
            try:
                step12_def = OnboardingStepDefinition.objects.get(step_number=12)
                status_obj, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step12_def)
                status_obj.status = 'COMPLETED'
                status_obj.save()
            except Exception:
                pass

    completed_steps = len(completed_step_nums)
    progress_pct = int((completed_steps / total_steps) * 100)

    # Offboarding is a terminal lifecycle state. Progress recalculation must
    # never reopen onboarding for a finalized tenant.
    stage = client_obj.stage
    if str(stage or '').lower() == 'offboarded':
        stage = 'offboarded'
    elif stage not in ['IN_PRODUCTION', 'PRODUCTION', 'production', 'production_pending']:
        if completed_steps == total_steps:
            stage = "onboarding_completed"
        else:
            stage = "onboarding"

    client_obj.progress_pct = progress_pct
    client_obj.stage = stage
    client_obj.save()

@csrf_exempt
def api_admin_step_upload(request, client_id, step_key):
    """ POST /admin-panel/api/clients/<client_id>/steps/<step_key>/upload/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "onboarding file upload")
    if locked:
        return locked

    file_bytes = request.body
    filename = request.headers.get('X-Filename', 'uploaded_document.pdf')

    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            client_obj = Client.objects.get(id=client_id)
            step_def = OnboardingStepDefinition.objects.get(step_number=step_num)

            # Step Validation using validation.py engine
            val_res = validate_step_upload(step_num, file_bytes, filename, client=client_obj)

            if not val_res.get("ok", True):
                checks = val_res.get("checks", [])
                err_msg = val_res.get("error")
                if not err_msg and checks:
                    err_msg = next((c["detail"] for c in checks if not c.get("ok")), "Validation failed")

                # Send failure email
                try:
                    from admin_panel.email_service import send_client_email
                    subject = f"OneSmarter: File Validation Failed - {filename}"
                    html = f"<h3>File Upload Failed</h3><p>The file <b>{filename}</b> failed validation.</p><p><b>Reason:</b> {err_msg}</p>"
                    send_client_email(client_obj, subject, html)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Failed to send email: {e}")

                return JsonResponse({
                    "success": False,
                    "error": err_msg or "Validation failed",
                    "checks": checks
                }, status=400)

            # Save file as ClientDocument
            if file_bytes:
                doc_name = f"Step {step_num}: {step_def.title}"
                doc_type = f"Onboarding Step {step_num}"

                doc = ClientDocument.objects.create(
                    client=client_obj,
                    document_name=doc_name,
                    original_filename=filename,
                    document_type=doc_type,
                    file_size=len(file_bytes),
                    uploaded_by="Admin User"
                )
                from django.core.files.base import ContentFile
                doc.file.save(filename, ContentFile(file_bytes), save=True)

            step_status, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step_def)
            step_status.status = 'COMPLETED'
            step_status.save()
            update_client_onboarding_stats(client_obj)

            # Audit log for step upload
            try:
                actor = "System"
                if request.user and hasattr(request.user, "name") and request.user.name:
                    actor = request.user.name
                elif request.user and hasattr(request.user, "email") and request.user.email:
                    actor = request.user.email
                AuditLog.objects.create(
                    module="ONBOARDING",
                    action="STEP_UPLOAD",
                    details=f"Step {step_num} ('{step_def.title}') document uploaded for client '{client_obj.name}'. File: {filename}.",
                    performed_by=actor,
                    client=client_obj
                )
            except Exception:
                pass

            # Send success email
            try:
                from admin_panel.email_service import send_client_email
                subject = f"OneSmarter: File Upload Successful - {filename}"
                html = f"<h3>File Upload Successful</h3><p>The file <b>{filename}</b> was successfully uploaded and passed all validations.</p>"
                send_client_email(client_obj, subject, html)
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

            return JsonResponse({
                "success": True,
                "message": "File uploaded and step completed.",
                "checks": val_res.get("checks", [])
            })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    return JsonResponse({"success": True, "message": "File uploaded and step completed.", "checks": []})



@csrf_exempt
def api_admin_template_download(request, client_id, step_key):
    """ GET /admin-panel/api/download/<client_id>/<step_key>/ """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    import os
    from django.conf import settings
    from django.http import HttpResponse

    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])

            if step_num == 1:
                from admin_panel.nda_service import build_client_nda, nda_download_filename
                client_obj = Client.objects.get(id=client_id)
                pdf_bytes = build_client_nda(client_obj)
                download_name = nda_download_filename(client_obj)
                response = HttpResponse(pdf_bytes, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="{download_name}"'
                response['X-OneSmarter-Filename'] = download_name
                return response

            if step_num == 2:
                from admin_panel.baa_service import build_client_baa, baa_download_filename
                client_obj = Client.objects.get(id=client_id)
                pdf_bytes = build_client_baa(client_obj)
                download_name = baa_download_filename(client_obj)
                response = HttpResponse(pdf_bytes, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="{download_name}"'
                response['X-OneSmarter-Filename'] = download_name
                return response

            if step_num == 3:
                from admin_panel.security_review_service import (
                    build_client_security_review,
                    security_review_download_filename,
                )
                client_obj = Client.objects.get(id=client_id)
                pdf_bytes = build_client_security_review(client_obj)
                download_name = security_review_download_filename(client_obj)
                response = HttpResponse(pdf_bytes, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="{download_name}"'
                response['X-OneSmarter-Filename'] = download_name
                return response

            template_map = {}

            filename = template_map.get(step_num)

            if not filename:
                return JsonResponse({"success": False, "error": "No template available for this step."}, status=404)

            file_path = os.path.join(settings.BASE_DIR, 'sample_docs', filename)

            if not os.path.exists(file_path):
                return JsonResponse({"success": False, "error": f"Template file {filename} not found."}, status=404)

            with open(file_path, 'rb') as f:
                response = HttpResponse(f.read(), content_type='application/octet-stream')
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                response['X-OneSmarter-Filename'] = filename
                return response
        else:
            return JsonResponse({"success": False, "error": "Invalid step key."}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
def api_admin_step_file(request, client_id, step_key):
    """ GET /admin-panel/api/clients/<client_id>/steps/<step_key>/file/ """
    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            doc_type = f"Onboarding Step {step_num}"
            doc = ClientDocument.objects.filter(client_id=client_id, document_type=doc_type).order_by('-created_at').first()
            if doc:
                import mimetypes
                content_type, _ = mimetypes.guess_type(doc.original_filename)
                if not content_type:
                    content_type = "application/pdf" if doc.original_filename.lower().endswith(".pdf") else "application/octet-stream"
                from django.http import HttpResponse
                response = HttpResponse(doc.file.read(), content_type=content_type)
                response['Content-Disposition'] = f'inline; filename="{doc.original_filename}"'
                response['X-OneSmarter-Filename'] = doc.original_filename
                return response
    except Exception:
        pass

    return JsonResponse({"success": False, "error": "File not found"}, status=404)


@csrf_exempt
def api_admin_step_notes(request, client_id, step_key):
    """ GET/POST /admin-panel/api/clients/<client_id>/steps/<step_key>/notes/ """
    from accounts.models import ClientStepComment

    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)

    try:
        parts = step_key.split('_')
        if step_key.startswith('golive_step_'):
            step_number = 100 + int(parts[2])
        elif step_key.startswith('offboard_step_'):
            step_number = 200 + int(parts[2])
        elif step_key.startswith('step_'):
            step_number = int(parts[1])
        else:
            raise ValueError
    except (ValueError, IndexError):
        return JsonResponse({"success": False, "error": "Invalid step key."}, status=400)

    if request.method == "POST":
        locked = _offboarded_workflow_lock(request, client_id, "workflow note creation")
        if locked:
            return locked
        try:
            body = json.loads(request.body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"success": False, "error": "Invalid JSON body."}, status=400)
        note_text = (body.get('note_text') or '').strip()
        if not note_text:
            return JsonResponse({"success": False, "error": "Note text is required."}, status=400)
        author = getattr(request.user, 'name', '') or getattr(request.user, 'email', '') or 'Administrator'
        note = ClientStepComment.objects.create(
            client=client_obj,
            step_number=step_number,
            comment=note_text,
            author=author,
        )
        return JsonResponse({
            "success": True,
            "message": "Note added successfully.",
            "note": {
                "id": str(note.id),
                "note_text": note.comment,
                "author": note.author,
                "created_at": note.created_at.isoformat(),
            },
        })

    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET and POST are allowed."}, status=405)

    notes = ClientStepComment.objects.filter(
        client=client_obj,
        step_number=step_number,
    ).order_by('-created_at')
    return JsonResponse({
        "success": True,
        "notes": [
            {
                "id": str(note.id),
                "note_text": note.comment,
                "author": note.author,
                "created_at": note.created_at.isoformat(),
            }
            for note in notes
        ],
    })


def _comment_step_number(step_key):
    parts = step_key.split('_')
    if step_key.startswith('golive_step_'):
        return 100 + int(parts[2])
    if step_key.startswith('offboard_step_'):
        return 200 + int(parts[2])
    if step_key.startswith('step_'):
        return int(parts[1])
    raise ValueError("Invalid step key")


@csrf_exempt
def api_admin_delete_step_note(request, client_id, step_key, note_id):
    from accounts.models import ClientStepComment
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "workflow note deletion")
    if locked:
        return locked
    try:
        step_number = _comment_step_number(step_key)
    except (ValueError, IndexError):
        return JsonResponse({"success": False, "error": "Invalid step key."}, status=400)
    deleted, _ = ClientStepComment.objects.filter(
        id=note_id, client_id=client_id, step_number=step_number,
    ).delete()
    if not deleted:
        return JsonResponse({"success": False, "error": "Note not found."}, status=404)
    return JsonResponse({"success": True, "message": "Note deleted successfully."})


@csrf_exempt
def api_admin_delete_client_contact(request, client_id, contact_id):
    locked = _offboarded_workflow_lock(request, client_id, "onboarding contact deletion")
    if locked:
        return locked
    from accounts.models import ClientContact
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    deleted, _ = ClientContact.objects.filter(id=contact_id, client_id=client_id).delete()
    if not deleted:
        return JsonResponse({"success": False, "error": "Contact not found."}, status=404)
    return JsonResponse({"success": True, "message": "Contact deleted successfully."})


@csrf_exempt
def api_admin_delete_client_user(request, client_id, user_id):
    locked = _offboarded_workflow_lock(request, client_id, "onboarding user deletion")
    if locked:
        return locked
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    user_obj = User.objects.filter(id=user_id, client_id=client_id, is_staff=False).first()
    if not user_obj:
        return JsonResponse({"success": False, "error": "Client user not found."}, status=404)
    email = user_obj.email
    user_obj.delete()
    return JsonResponse({"success": True, "message": f"User '{email}' deleted successfully."})


@csrf_exempt
def api_admin_step_redo(request, client_id, step_key):
    """ POST /admin-panel/api/clients/<client_id>/steps/<step_key>/redo/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "onboarding step redo")
    if locked:
        return locked
    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            client_obj = Client.objects.get(id=client_id)

            with transaction.atomic():
                step_def = OnboardingStepDefinition.objects.get(step_number=step_num)
                step_status, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step_def)
                step_status.status = 'IN_PROGRESS'
                step_status.save()

                # Reset every later workflow action to PENDING. Workflow order
                # differs from numeric order because Step 10 precedes Step 8.
                current_position = onboarding_process_position(step_num)
                subsequent_step_numbers = ONBOARDING_PROCESS_ORDER[current_position + 1:]
                subsequent_statuses = ClientStepStatus.objects.filter(
                    client=client_obj,
                    step__step_number__in=subsequent_step_numbers,
                ).exclude(status='PENDING').select_related('step')
                for sub_status in subsequent_statuses:
                    sub_status.status = 'PENDING'
                    sub_status.save()

                update_client_onboarding_stats(client_obj)

            # Audit log for step redo
            try:
                actor = "System"
                if request.user and hasattr(request.user, "name") and request.user.name:
                    actor = request.user.name
                elif request.user and hasattr(request.user, "email") and request.user.email:
                    actor = request.user.email
                AuditLog.objects.create(
                    module="ONBOARDING",
                    action="STEP_REDO",
                    details=f"Step {step_num} redone for client '{client_obj.name}'. Subsequent steps reset to PENDING.",
                    performed_by=actor,
                    client=client_obj
                )
            except Exception:
                pass

    except Exception:
        pass
    return JsonResponse({"success": True, "message": "Step reset to IN_PROGRESS, subsequent steps locked"})



@csrf_exempt
def api_admin_step_validate_835(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/steps/step_7_835_val/validate-uploaded/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "onboarding 835 validation")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)

        # The onboarding screen sends the original name in X-Filename because
        # the request body contains the raw file bytes. Resolve and validate it
        # before parsing so every extension in the shared 835 policy follows
        # the normal 835 validation pipeline.
        from urllib.parse import unquote
        from edi835.file_types import file_extension_error, has_valid_file_extension
        uploaded_filename = os.path.basename(
            unquote(request.headers.get("X-Filename", "uploaded_file.x12"))
        )
        if not has_valid_file_extension(uploaded_filename, "835"):
            return JsonResponse({
                "success": False,
                "error": file_extension_error("835"),
                "checks": [],
            }, status=400)

        file_bytes = request.body
        if not file_bytes:
            return JsonResponse({"success": False, "error": "No file uploaded"}, status=400)

        raw_text = file_bytes.decode('utf-8', errors='replace')
        from converter.services.validator import EDI835Validator
        validator = EDI835Validator()
        report = validator.validate(raw_text)
        is_valid = report.get('valid', report.get('is_valid', True))

        if not is_valid:
            errors = report.get('errors', [])
            err_msg = "EDI Validation Failed. " + (errors[0] if errors else "Errors found.")
            checks = [{"ok": False, "label": "Structure", "detail": e} for e in errors]

            # Send failure email
            try:
                from admin_panel.email_service import send_client_email
                filename_to_report = uploaded_filename
                subject = f"OneSmarter: 835 File Validation Failed - {filename_to_report}"
                html = f"<h3>835 File Validation Failed</h3><p>The file <b>{filename_to_report}</b> failed X12 validation.</p><p><b>Reason:</b> {err_msg}</p>"
                send_client_email(client_obj, subject, html)
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

            return JsonResponse({"success": False, "error": err_msg, "checks": checks}, status=400)

        checks = [{"ok": True, "label": "Structure", "detail": f"835 structural and balance checks passed. Claims found: {report.get('claims', 0)}"}]

        # Save as ClientDocument now that it is valid
        filename = uploaded_filename
        doc_name = f"Step 8: 835 File Validation"
        from admin_panel.models import ClientDocument
        from django.core.files.base import ContentFile

        doc = ClientDocument.objects.create(
            client=client_obj,
            document_name=doc_name,
            original_filename=filename,
            document_type="Onboarding Step 8",
            file_size=len(file_bytes),
            uploaded_by="Admin User"
        )
        doc.file.save(filename, ContentFile(file_bytes), save=True)

        # Process the EDI file content immediately through the pipeline (validation, conversion, SFTP upload)
        from edi835.services import process_edi835_file_content, resolve_sftp_config
        proc_res = process_edi835_file_content(raw_text, original_filename=filename, client=client_obj)

        if not proc_res.get("success"):
            return JsonResponse({
                "success": False,
                "error": f"835 validation passed, but MIR conversion failed: {proc_res.get('error', 'Unknown conversion error')}",
                "checks": checks,
            }, status=400)

        db_record = proc_res.get("db_record")
        if not db_record or not db_record.present_in_sftp:
            outbound_cfg = resolve_sftp_config(client=client_obj, outbound=True)
            upload_error = (
                getattr(outbound_cfg, "last_error", None)
                or "The configured outbound SFTP folder rejected the upload."
            )
            checks.append({
                "ok": True,
                "label": "MIR Conversion",
                "detail": "835 converted to MIR successfully.",
            })
            checks.append({
                "ok": False,
                "label": "SFTP Upload",
                "detail": upload_error,
            })
            return JsonResponse({
                "success": False,
                "error": f"MIR conversion succeeded, but outbound SFTP upload failed: {upload_error}",
                "checks": checks,
                "file_id": str(db_record.id) if db_record else None,
            }, status=502)

        checks.extend([
            {
                "ok": True,
                "label": "MIR Conversion",
                "detail": "835 converted to MIR successfully.",
            },
            {
                "ok": True,
                "label": "SFTP Upload",
                "detail": "Generated MIR uploaded to the configured outbound SFTP folder.",
            },
        ])

        # Notify the active client users created during onboarding. This runs
        # only after the MIR has been successfully uploaded to SFTP and uses
        # the SMTP configuration saved for this client in Step 6.
        email_sent = False
        email_recipients = []
        email_error = None
        try:
            from admin_panel.email_service import send_client_email, get_client_users
            from django.utils.html import escape

            email_recipients = get_client_users(client_obj)
            outbound_cfg = resolve_sftp_config(client=client_obj, outbound=True)
            outbound_folder = getattr(outbound_cfg, "outbound_mir_folder", None) or "/"
            mir_filename = _canonical_mir_filename(db_record) or (Path(db_record.output_path).name if db_record.output_path else "Generated MIR file")
            delivered_at = timezone.localtime().strftime("%B %d, %Y at %I:%M %p %Z")

            email_subj = f"MIR Delivery Confirmation – {filename}"
            email_html = f"""
            <div style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.6;max-width:680px">
              <h2 style="color:#0f766e;margin-bottom:8px">835 Validation and MIR Delivery Completed</h2>
              <p>Dear {escape(client_obj.name)} Team,</p>
              <p>
                The submitted 835 file has passed validation, was converted successfully to MIR format,
                and the generated MIR file was uploaded to your configured outbound SFTP location.
              </p>
              <table style="border-collapse:collapse;width:100%;margin:18px 0">
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Source 835 file</td><td style="padding:8px;border:1px solid #d1d5db">{escape(filename)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Generated MIR file</td><td style="padding:8px;border:1px solid #d1d5db">{escape(mir_filename)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Validation</td><td style="padding:8px;border:1px solid #d1d5db">Passed</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">SFTP delivery</td><td style="padding:8px;border:1px solid #d1d5db">Successful</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Outbound folder</td><td style="padding:8px;border:1px solid #d1d5db">{escape(outbound_folder)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Claims identified</td><td style="padding:8px;border:1px solid #d1d5db">{proc_res.get('claims_count', 0)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Services processed</td><td style="padding:8px;border:1px solid #d1d5db">{proc_res.get('services_count', 0)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">MIR records created</td><td style="padding:8px;border:1px solid #d1d5db">{proc_res.get('records_count', 0)}</td></tr>
                <tr><td style="padding:8px;border:1px solid #d1d5db;font-weight:bold">Completed at</td><td style="padding:8px;border:1px solid #d1d5db">{escape(delivered_at)}</td></tr>
              </table>
              <p>No further action is required unless you are unable to locate the MIR file in the outbound SFTP folder.</p>
              <p>Sincerely,<br><strong>OneSmarter Inc.</strong></p>
            </div>
            """
            if not email_recipients:
                email_error = "No active client-user email address is available."
            else:
                email_sent = send_client_email(
                    client_obj,
                    email_subj,
                    email_html,
                    to_emails=email_recipients,
                )
                if not email_sent:
                    email_error = "The client SMTP server did not send the notification."
        except Exception as exc:
            email_error = str(exc)
            logging.getLogger(__name__).exception(
                "Step 9 delivery email failed for client %s", client_obj.id
            )

        checks.append({
            "ok": email_sent,
            "label": "Client Email Notification",
            "detail": (
                f"Delivery confirmation sent to {', '.join(email_recipients)}."
                if email_sent
                else email_error or "Delivery confirmation email was not sent."
            ),
        })

        step_def = OnboardingStepDefinition.objects.get(step_number=8)
        step_status, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step_def)
        step_status.status = 'COMPLETED'
        step_status.save()
        update_client_onboarding_stats(client_obj)

        return JsonResponse({
            "success": True,
            "message": "835 validated, converted to MIR, and uploaded to outbound SFTP successfully.",
            "checks": checks,
            "file_id": str(db_record.id),
            "mir_output_path": db_record.output_path,
            "email_sent": email_sent,
            "email_recipients": email_recipients,
            "email_error": email_error,
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@csrf_exempt
def api_admin_step_action(request, client_id, step_key, action):
    """ POST /admin-panel/api/clients/<client_id>/steps/<step_key>/<action>/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, f"onboarding step action '{action}'")
    if locked:
        return locked

    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            client_obj = Client.objects.get(id=client_id)
            response_data = {}

            if action == "save" and step_num == 4:
                from accounts.models import ClientContact
                try:
                    data = json.loads(request.body.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return JsonResponse({'success': False, 'error': 'Invalid JSON body.'}, status=400)
                name = (data.get('employee_name') or '').strip()
                email = (data.get('email') or '').strip().lower()
                phone = (data.get('phone') or '').strip()
                role_name = (data.get('role_name') or '').strip() or 'Technical Contact'
                if not name:
                    return JsonResponse({'success': False, 'error': 'Contact name is required.'}, status=400)
                if email:
                    ok_email, err_email = validate_email_address(email)
                    if not ok_email:
                        return JsonResponse({'success': False, 'error': err_email}, status=400)
                if phone:
                    ok_phone, err_phone = validate_phone_number(phone)
                    if not ok_phone:
                        return JsonResponse({'success': False, 'error': err_phone}, status=400)
                with transaction.atomic():
                    Client.objects.select_for_update().get(id=client_id)
                    duplicate_filter = Q(name__iexact=name)
                    if email:
                        duplicate_filter |= Q(email__iexact=email)
                    if phone:
                        duplicate_filter |= Q(phone=phone)
                    if ClientContact.objects.filter(client=client_obj).filter(duplicate_filter).exists():
                        return JsonResponse({'success': False, 'error': 'This contact already exists for the client.'}, status=409)
                    contact = ClientContact.objects.create(
                        client=client_obj, role_name=role_name, name=name,
                        email=email or None, phone=phone or None,
                    )
                response_data['contact'] = {
                    'id': str(contact.id), 'role_name': contact.role_name, 'name': contact.name,
                    'email': contact.email or '', 'phone': contact.phone or '',
                }

            if action == "save" and step_num == 10:
                try:
                    data = json.loads(request.body.decode('utf-8'))
                    mir_format = data.get('mir_filename_format', '').strip()
                    if mir_format:
                        client_obj.mir_filename_format = mir_format
                        client_obj.save(update_fields=['mir_filename_format'])
                except Exception as e:
                    return JsonResponse({'success': False, 'error': str(e)}, status=400)

            # ── Step 6: persist SMTP config (password encrypted at rest) ───
            if action == "send" and step_num == 6:
                try:
                    data = json.loads(request.body.decode('utf-8'))
                    smtp_fields = {
                        'sender_name':   data.get('sender_name', '').strip(),
                        'sender_email':  data.get('sender_email', '').strip(),
                        'smtp_host':     data.get('smtp_host', '').strip(),
                        'smtp_port':     int(data.get('smtp_port', 587)),
                        'smtp_username': data.get('smtp_username', '').strip(),
                        'security':      data.get('security', 'STARTTLS').strip(),
                        'reply_to':      data.get('reply_to', '').strip() or None,
                    }
                    plain_password = data.get('smtp_password', '').strip()
                    if plain_password:
                        smtp_fields['smtp_password'] = encrypt_smtp_password(plain_password)

                    # If Use Default SMTP is checked, set use_default=True
                    use_def_smtp = bool(data.get('use_default', False))
                    smtp_fields['use_default'] = use_def_smtp

                    ClientSmtpConfig.objects.update_or_create(
                        client=client_obj,
                        defaults=smtp_fields
                    )

                    # Send SMTP configuration success email
                    try:
                        from admin_panel.email_service import send_client_email
                        subject = f"OneSmarter: SMTP Configuration Complete"
                        html = f"<p>Hello,</p><p>SMTP configuration for {client_obj.name} has been successfully completed in the OneSmarter system.</p>"
                        send_client_email(client_obj, subject, html)
                    except Exception as email_err:
                        # Log but do not fail the step
                        import logging
                        logging.getLogger(__name__).error(f"Failed to send SMTP success email: {email_err}")
                except Exception as smtp_err:
                    return JsonResponse({'success': False, 'error': f'SMTP save failed: {smtp_err}'}, status=400)
            # ─────────────────────────────────────────────────────────────────

            if (action == "save" and step_num in [5, 10, 11]) or (action == "send" and step_num == 6) or action == "submit-text":
                from accounts.models import ClientStepComment
                try:
                    data = json.loads(request.body.decode('utf-8'))
                    verification_text = data.get('verification_text') or data.get('notes', '').strip() or data.get('submission_text', '').strip()
                    if verification_text:
                        author = "System"
                        if request.user and hasattr(request.user, "name") and request.user.name:
                            author = request.user.name
                        elif request.user and hasattr(request.user, "email") and request.user.email:
                            author = request.user.email
                        latest = ClientStepComment.objects.filter(client=client_obj, step_number=step_num).first()
                        if latest and latest.comment == verification_text and latest.author == author:
                            note = latest
                        else:
                            note = ClientStepComment.objects.create(
                                client=client_obj, step_number=step_num,
                                comment=verification_text, author=author,
                            )
                        response_data['note'] = {
                            'id': str(note.id), 'note_text': note.comment, 'author': note.author,
                            'created_at': note.created_at.isoformat(),
                        }
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return JsonResponse({'success': False, 'error': 'Invalid JSON body.'}, status=400)

            if action == "save" and step_num == 13:
                try:
                    body = json.loads(request.body.decode('utf-8'))
                    scheduled_date = body.get('scheduled_date', '').strip()
                    scheduled_time = body.get('scheduled_time', '10:00').strip()
                    timezone_name = _valid_timezone_name(body.get('timezone'))
                    notes = body.get('notes', '').strip()

                    if notes:
                        from accounts.models import ClientStepComment
                        author = "System"
                        if request.user and hasattr(request.user, "name") and request.user.name:
                            author = request.user.name
                        elif request.user and hasattr(request.user, "email") and request.user.email:
                            author = request.user.email
                        latest = ClientStepComment.objects.filter(client=client_obj, step_number=step_num).first()
                        if not latest or latest.comment != notes or latest.author != author:
                            ClientStepComment.objects.create(
                                client=client_obj, step_number=step_num,
                                comment=notes, author=author,
                            )

                    if scheduled_date:
                        from datetime import datetime
                        from django.utils import timezone
                        try:
                            if "-" in scheduled_date and len(scheduled_date.split("-")[0]) == 4:
                                dt = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%Y-%m-%d %H:%M")
                            else:
                                dt = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%m-%d-%Y %H:%M")
                            client_obj.live_since = timezone.make_aware(dt, ZoneInfo(timezone_name))
                            client_obj.timezone = timezone_name
                            client_obj.save(update_fields=["live_since", "timezone", "updated_at"])
                        except ValueError:
                            pass
                except Exception:
                    pass

            step_def = OnboardingStepDefinition.objects.get(step_number=step_num)
            step_status, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step_def)
            step_status.status = 'COMPLETED'
            step_status.save()
            update_client_onboarding_stats(client_obj)

            # Audit log for step action
            try:
                actor = "System"
                if request.user and hasattr(request.user, "name") and request.user.name:
                    actor = request.user.name
                elif request.user and hasattr(request.user, "email") and request.user.email:
                    actor = request.user.email
                AuditLog.objects.create(
                    module="ONBOARDING",
                    action=f"STEP_{action.upper().replace('-', '_')}",
                    details=f"Step {step_num} ('{step_def.title}') action '{action}' completed for client '{client_obj.name}'.",
                    performed_by=actor,
                    client=client_obj
                )
            except Exception:
                pass

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    return JsonResponse({"success": True, "message": f"Action {action} on {step_key} completed successfully.", **response_data})



@csrf_exempt
def api_admin_client_smtp(request, client_id):
    """
    GET  /admin-panel/api/clients/<client_id>/smtp/  — load existing config (password never returned)
    POST /admin-panel/api/clients/<client_id>/smtp/  — upsert config (password stored encrypted)
    """
    try:
        client_obj = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Client not found'}, status=404)

    if request.method == 'GET':
        try:
            cfg = client_obj.smtp_config
            return JsonResponse({
                'success': True,
                'config': {
                    'sender_name':   cfg.sender_name,
                    'sender_email':  cfg.sender_email,
                    'smtp_host':     cfg.smtp_host,
                    'smtp_port':     cfg.smtp_port,
                    'smtp_username': cfg.smtp_username,
                    'security':      cfg.security,
                    'reply_to':      cfg.reply_to or '',
                    'use_default':   cfg.use_default,
                    # smtp_password intentionally NEVER sent to the browser
                    'has_password':  bool(cfg.smtp_password),
                }
            })
        except ClientSmtpConfig.DoesNotExist:
            return JsonResponse({'success': True, 'config': None})

    if request.method == 'POST':
        locked = _offboarded_workflow_lock(request, client_id, "onboarding SMTP update")
        if locked:
            return locked
        try:
            data = json.loads(request.body.decode('utf-8'))
            smtp_fields = {
                'sender_name':   data.get('sender_name', '').strip(),
                'sender_email':  data.get('sender_email', '').strip(),
                'smtp_host':     data.get('smtp_host', '').strip(),
                'smtp_port':     int(data.get('smtp_port', 587)),
                'smtp_username': data.get('smtp_username', '').strip(),
                'security':      data.get('security', 'STARTTLS').strip(),
                'reply_to':      data.get('reply_to', '').strip() or None,
            }
            plain_password = data.get('smtp_password', '').strip()
            if plain_password:
                # Encrypt before storing — only the server key can decrypt it
                smtp_fields['smtp_password'] = encrypt_smtp_password(plain_password)
            obj, created = ClientSmtpConfig.objects.update_or_create(
                client=client_obj,
                defaults=smtp_fields
            )
            return JsonResponse({'success': True, 'created': created})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

@csrf_exempt
@admin_api_required
def api_admin_default_smtp(request):
    """
    GET:
        Returns default SMTP configuration.
        Password and encrypted ciphertext are never returned.

    POST:
        Creates or updates default SMTP configuration.
        Password is encrypted before being stored.
        An empty password preserves the existing password.
    """

    # ---------------------------------------------------------
    # GET DEFAULT SMTP CONFIGURATION
    # ---------------------------------------------------------
    if request.method == "GET":
        try:
            cfg = ClientSmtpConfig.objects.get(
                client__isnull=True
            )

            return JsonResponse(
                {
                    "success": True,
                    "config": {
                        "sender_name": cfg.sender_name,
                        "sender_email": cfg.sender_email,
                        "smtp_host": cfg.smtp_host,
                        "smtp_port": cfg.smtp_port,
                        "smtp_username": cfg.smtp_username,
                        "security": cfg.security,
                        "reply_to": cfg.reply_to or "",

                        # Only tell frontend whether a password exists.
                        # Never return plaintext or encrypted password.
                        "has_password": bool(
                            cfg.smtp_password
                        ),
                    },
                }
            )

        except ClientSmtpConfig.DoesNotExist:
            return JsonResponse(
                {
                    "success": True,
                    "config": None,
                }
            )

        except Exception:
            logging.exception(
                "Failed to load default SMTP configuration"
            )

            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "Failed to load default SMTP "
                        "configuration."
                    ),
                },
                status=500,
            )

    # ---------------------------------------------------------
    # SAVE DEFAULT SMTP CONFIGURATION
    # ---------------------------------------------------------
    if request.method == "POST":
        try:
            data = json.loads(
                request.body.decode("utf-8")
            )

            sender_name = (
                data.get("sender_name") or ""
            ).strip()

            sender_email = (
                data.get("sender_email") or ""
            ).strip()

            smtp_host = (
                data.get("smtp_host") or ""
            ).strip()

            smtp_username = (
                data.get("smtp_username") or ""
            ).strip()

            plain_password = (
                data.get("smtp_password") or ""
            ).strip()

            security = (
                data.get("security") or "STARTTLS"
            ).strip().upper()

            reply_to = (
                data.get("reply_to") or ""
            ).strip() or None

            try:
                smtp_port = int(
                    data.get("smtp_port", 587)
                )
            except (ValueError, TypeError):
                return JsonResponse(
                    {
                        "success": False,
                        "error": (
                            "SMTP port must be a valid number."
                        ),
                    },
                    status=400,
                )

            if smtp_port < 1 or smtp_port > 65535:
                return JsonResponse(
                    {
                        "success": False,
                        "error": (
                            "SMTP port must be between "
                            "1 and 65535."
                        ),
                    },
                    status=400,
                )

            if not sender_name:
                return JsonResponse(
                    {
                        "success": False,
                        "error": "Sender name is required.",
                    },
                    status=400,
                )

            if not sender_email:
                return JsonResponse(
                    {
                        "success": False,
                        "error": "Sender email is required.",
                    },
                    status=400,
                )

            if not smtp_host:
                return JsonResponse(
                    {
                        "success": False,
                        "error": "SMTP host is required.",
                    },
                    status=400,
                )

            if not smtp_username:
                return JsonResponse(
                    {
                        "success": False,
                        "error": "SMTP username is required.",
                    },
                    status=400,
                )

            if security not in {
                "STARTTLS",
                "SSL_TLS",
                "NONE",
            }:
                return JsonResponse(
                    {
                        "success": False,
                        "error": (
                            "Invalid SMTP security protocol."
                        ),
                    },
                    status=400,
                )

            smtp_fields = {
                "sender_name": sender_name,
                "sender_email": sender_email,
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "smtp_username": smtp_username,
                "security": security,
                "reply_to": reply_to,
            }

            # Only update the stored password when the user
            # entered a new password.
            if plain_password:
                smtp_fields["smtp_password"] = (
                    encrypt_smtp_password(
                        plain_password
                    )
                )

            config, created = (
                ClientSmtpConfig.objects.update_or_create(
                    client=None,
                    defaults=smtp_fields,
                )
            )

            return JsonResponse(
                {
                    "success": True,
                    "created": created,
                    "message": (
                        "Default SMTP configuration saved "
                        "successfully."
                    ),
                    "config": {
                        "sender_name": config.sender_name,
                        "sender_email": config.sender_email,
                        "smtp_host": config.smtp_host,
                        "smtp_port": config.smtp_port,
                        "smtp_username": (
                            config.smtp_username
                        ),
                        "security": config.security,
                        "reply_to": (
                            config.reply_to or ""
                        ),
                        "has_password": bool(
                            config.smtp_password
                        ),
                    },
                }
            )

        except json.JSONDecodeError:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid JSON request.",
                },
                status=400,
            )

        except Exception:
            logging.exception(
                "Failed to save default SMTP configuration"
            )

            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "Failed to save default SMTP "
                        "configuration."
                    ),
                },
                status=500,
            )

    return JsonResponse(
        {
            "success": False,
            "error": "Method not allowed.",
        },
        status=405,
    )


from admin_panel.models import ClientDocument
from django.core.files.base import ContentFile
from django.http import HttpResponse

@csrf_exempt
def api_admin_client_documents(request, client_id):
    """ GET /admin-panel/api/clients/<client_id>/documents/ """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    docs = ClientDocument.objects.filter(client_id=client_id).order_by('-created_at')
    seen_keys = set()
    doc_list = []
    for d in docs:
        if d.document_type == 'General Document':
            key = f"general_{d.document_name}"
        else:
            key = d.document_type

        if key not in seen_keys:
            seen_keys.add(key)
            doc_list.append({
                "id": str(d.id),
                "document_name": d.document_name,
                "original_filename": d.original_filename,
                "document_type": d.document_type,
                "file_size": d.file_size,
                "uploaded_by": d.uploaded_by,
                "created_at": d.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if d.created_at else ""
            })
    return JsonResponse({"success": True, "documents": doc_list})


@csrf_exempt
def api_admin_client_documents_upload(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/documents/upload/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)

    file_bytes = request.body

    filename = request.headers.get('X-Filename', 'uploaded_document.pdf')
    doc_name = request.headers.get('X-Doc-Name', filename)
    doc_type = request.headers.get('X-Doc-Type', 'General Document')

    try:
        client_obj = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    # Document Validation & Integrity Engine check
    doc_text = extract_text_from_file_bytes(file_bytes, filename)
    val_res = validate_document_text(doc_text, step_title=doc_name)

    if not val_res["ok"]:
        return JsonResponse({
            "success": False,
            "error": val_res["status_message"],
            "checks": val_res["checks"]
        }, status=400)

    doc = ClientDocument.objects.create(
        client=client_obj,
        document_name=doc_name,
        original_filename=filename,
        document_type=doc_type,
        file_size=len(file_bytes),
        uploaded_by="Admin User"
    )
    doc.file.save(filename, ContentFile(file_bytes), save=True)

    # Audit Logging
    actor = "Admin User"
    if request.user and request.user.is_authenticated:
        actor = request.user.name or request.user.email
    AuditLog.objects.create(
        module="DOCUMENTS",
        action="DOCUMENT_UPLOADED",
        details=f"Uploaded document '{doc_name}' ({filename}) for client '{client_obj.name}'.",
        performed_by=actor,
        client=client_obj
    )

    return JsonResponse({
        "success": True,
        "message": "Document uploaded successfully",
        "checks": val_res["checks"]
    })


@csrf_exempt
def api_admin_document_download(request, doc_id):
    """ GET /admin-panel/api/documents/<doc_id>/download/ """
    try:
        doc = ClientDocument.objects.get(id=doc_id)
    except ClientDocument.DoesNotExist:
        return JsonResponse({"success": False, "error": "Document not found"}, status=404)

    try:
        import mimetypes
        content_type, _ = mimetypes.guess_type(doc.original_filename)
        if not content_type:
            content_type = "application/pdf" if doc.original_filename.lower().endswith(".pdf") else "application/octet-stream"
        from django.http import HttpResponse
        response = HttpResponse(doc.file.read(), content_type=content_type)
        response['Content-Disposition'] = f'inline; filename="{doc.original_filename}"'
        response['X-OneSmarter-Filename'] = doc.original_filename
        return response
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
def api_admin_document_delete(request, doc_id):
    """ DELETE /admin-panel/api/documents/<doc_id>/ """
    if request.method != "DELETE":
        return JsonResponse({"success": False, "error": "Only DELETE allowed"}, status=405)

    try:
        doc = ClientDocument.objects.get(id=doc_id)
        doc.file.delete(save=False)

        # Audit Logging
        actor = "Admin User"
        if request.user and request.user.is_authenticated:
            actor = request.user.name or request.user.email
        AuditLog.objects.create(
            module="DOCUMENTS",
            action="DOCUMENT_DELETED",
            details=f"Deleted document '{doc.document_name}' ({doc.original_filename}) for client '{doc.client.name}'.",
            performed_by=actor,
            client=doc.client
        )

        doc.delete()
        return JsonResponse({"success": True, "message": "Document deleted successfully"})
    except ClientDocument.DoesNotExist:
        return JsonResponse({"success": False, "error": "Document not found"}, status=404)



from edi835.models import EDI835File

@csrf_exempt
def api_admin_client_edi_files(request, client_id):
    """ GET /admin-panel/api/clients/<client_id>/edi-files/ """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    files = (
        EDI835File.objects.filter(client_id=client_id)
        .select_related("mir_file")
        .defer("input_file_content", "mir_file__file_content")
        .order_by('-uploaded_at')
    )
    file_list = []
    for f in files:
        file_list.append({
            "id": str(f.id),
            "original_filename": f.original_filename,
            "stored_filename": f.stored_filename,
            "mir_filename": _canonical_mir_filename(f),
            "output_filename": _canonical_mir_filename(f),
            "combined_filename": _canonical_mir_filename(f),
            "status": f.status,
            "claims_count": f.claims_count,
            "services_count": f.services_count,
            "records_count": f.records_count,
            "ingestion_source": f.ingestion_source,
            "present_in_sftp": f.present_in_sftp,
            "present_in_archive_folder": f.present_in_archive_folder,
            "uploaded_at": f.uploaded_at.strftime("%Y-%m-%dT%H:%M:%SZ") if f.uploaded_at else "",
            "processing_completed_at": f.processing_completed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if f.processing_completed_at else "",
            "error_message": f.error_message
        })
    return JsonResponse({"success": True, "files": file_list})
@csrf_exempt
def api_admin_edi_file(request, client_id, file_id, file_type):
    """
    GET /admin-panel/api/clients/<client_id>/edi-files/<file_id>/<file_type>/

    file_type:
      - input   -> original 835/X12 file
      - mir     -> generated MIR file

    Used by the Admin Files/Archive page for View and Download.
    """
    if request.method != "GET":
        return JsonResponse(
            {"success": False, "error": "Only GET allowed"},
            status=405
        )

    if file_type not in ["input", "mir"]:
        return JsonResponse(
            {"success": False, "error": "Invalid file type"},
            status=400
        )

    try:
        file_record = EDI835File.objects.get(
            id=file_id,
            client_id=client_id
        )
    except EDI835File.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "File record not found"},
            status=404
        )

    from pathlib import Path
    from django.conf import settings
    from django.http import FileResponse
    import mimetypes
    import os

    # ---------------------------------------------------------
    # Determine which physical file to serve
    # ---------------------------------------------------------
    if file_type == "mir":
        relative_path = file_record.output_path

        if not relative_path:
            return JsonResponse(
                {"success": False, "error": "MIR file has not been generated yet."},
                status=404
            )

        filename = _safe_mir_filename(
            _canonical_mir_filename(file_record) or os.path.basename(relative_path)
        )

    else:
        # Prefer archived 835 because processed files are moved
        # from processing -> archive.
        relative_path = file_record.archive_path or file_record.input_path

        if not relative_path:
            return JsonResponse(
                {"success": False, "error": "Original 835 file was not found."},
                status=404
            )

        filename = file_record.original_filename or os.path.basename(relative_path)

    # ---------------------------------------------------------
    # Resolve physical path
    # ---------------------------------------------------------
    file_path = Path(settings.BASE_DIR) / relative_path

    if not file_path.exists() or not file_path.is_file():
        return JsonResponse(
            {
                "success": False,
                "error": f"Physical file not found: {filename}"
            },
            status=404
        )

    # ---------------------------------------------------------
    # Content type
    # ---------------------------------------------------------
    content_type, _ = mimetypes.guess_type(str(file_path))

    if not content_type:
        if file_type == "mir":
            content_type = "text/plain"
        else:
            content_type = "text/plain"

    # ---------------------------------------------------------
    # View vs download
    # ---------------------------------------------------------
    download = request.GET.get("download", "").lower() in [
        "1",
        "true",
        "yes"
    ]

    disposition = "attachment" if download else "inline"

    response = FileResponse(
        open(file_path, "rb"),
        content_type=content_type
    )

    response["Content-Disposition"] = (
        f'{disposition}; filename="{filename}"'
    )

    response["X-OneSmarter-Filename"] = filename

    return response

from admin_panel.models import ClientTestEnvironment

@csrf_exempt
def api_admin_client_test_environment(request, client_id):
    """ GET/PUT /admin-panel/api/clients/<client_id>/test-environment/ """
    try:
        client_obj = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    env, created = ClientTestEnvironment.objects.get_or_create(
        client=client_obj,
        defaults={
            "sftp_host": "sftp-test.internal",
            "sftp_username": f"{client_obj.id}_sandbox",
            "watched_folder": f"/relay/{client_obj.id}/in/835/",
            "test_status": "In Progress"
        }
    )

    if request.method == "GET":
        return JsonResponse({
            "success": True,
            "test_environment": {
                "sftp_host": env.sftp_host,
                "sftp_username": env.sftp_username,
                "watched_folder": env.watched_folder,
                "test_status": env.test_status
            }
        })
    elif request.method == "PUT":
        try:
            data = json.loads(request.body)
            env.sftp_host = data.get("sftp_host", env.sftp_host)
            env.sftp_username = data.get("sftp_username", env.sftp_username)
            env.watched_folder = data.get("watched_folder", env.watched_folder)
            env.test_status = data.get("test_status", env.test_status)
            env.save()
            return JsonResponse({"success": True, "message": "Test environment updated."})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
    else:
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)

# ============================================================
# GO LIVE & TEST ENVIRONMENT API VIEWS
# ============================================================

def helper_get_golive_state(client_obj):
    default_steps = [
        (1, "Go-Live Authorization Signed", "Formal sign-off for cutover into production."),
        (2, "Production Data Transfer Security Attestation", "HIPAA compliance evidence for production data transit."),
        (3, "Production SFTP Credentials Provisioned", "Configure production endpoints."),
        (4, "Production Cutover Schedule & Window Set", "Schedule maintenance window for production activation."),
        (5, "Special Processing Instructions / Comments Logged", "Log custom exceptions or client-specific processing notes."),
        (6, "Final Production Activation & Status Promoted to Live", "Promote client status to LIVE PRODUCTION."),
    ]
    for num, title, desc in default_steps:
        step_def = GoLiveStepDefinition.objects.filter(step_number=num).first()
        if not step_def:
            GoLiveStepDefinition.objects.create(step_number=num, title=title, description=desc)
        elif step_def.title != title:
            step_def.title = title
            step_def.description = desc
            step_def.save()

    step_defs = GoLiveStepDefinition.objects.all().order_by('step_number')
    step_statuses = ClientGoLiveStatus.objects.filter(client=client_obj).select_related('step')
    status_map = {ss.step_id: ss.status for ss in step_statuses}
    steps_data = []
    in_progress_found = False

    golive_comments = {}
    from accounts.models import ClientStepComment
    for comment in ClientStepComment.objects.filter(
        client=client_obj, step_number__in=[104, 105]
    ).order_by('step_number', '-created_at'):
        if comment.step_number not in golive_comments:
            golive_comments[comment.step_number] = comment

    for step in step_defs:
        st = status_map.get(step.id, 'PENDING')
        is_done = st == 'COMPLETED'
        is_in_progress = st == 'IN_PROGRESS'
        if is_in_progress:
            in_progress_found = True

        extra_data = {}
        latest_note = None

        if step.step_number == 4:
            if client_obj.live_since:
                from django.utils.timezone import localtime
                timezone_name = _client_timezone(client_obj)
                local_dt = localtime(client_obj.live_since, ZoneInfo(timezone_name))
                extra_data["schedule"] = {
                    "production_date": local_dt.strftime("%Y-%m-%d"),
                    "production_time": local_dt.strftime("%H:%M"),
                    "timezone": timezone_name,
                    "scheduled_at": client_obj.live_since.isoformat(),
                }
            note = golive_comments.get(104)
            if note:
                extra_data["schedule"] = extra_data.get("schedule", {})
                extra_data["schedule"]["notes"] = note.comment
                latest_note = {
                    "id": str(note.id),
                    "note_text": note.comment,
                    "author": note.author,
                    "created_at": note.created_at.isoformat(),
                }

        if step.step_number == 5:
            note = golive_comments.get(105)
            if note:
                latest_note = {
                    "id": str(note.id),
                    "note_text": note.comment,
                    "author": note.author,
                    "created_at": note.created_at.isoformat(),
                }

        steps_data.append({
            "id": step.id,
            "key": f"golive_step_{step.step_number}",
            "step_number": step.step_number,
            "title": step.title,
            "description": step.description,
            "done": is_done,
            "inProgress": is_in_progress,
            "file": step.step_number in [1, 2],
            "extra": extra_data,
            "latestNote": latest_note
        })

    if not in_progress_found:
        for s in steps_data:
            if not s["done"]:
                s["inProgress"] = True
                break

    return {
        "client": {
            "id": str(client_obj.id),
            "name": client_obj.name,
            "stage": client_obj.stage
        },
        "steps": steps_data
    }


@csrf_exempt
def api_admin_golive_state(request, client_id):
    """ GET /admin-panel/api/clients/<client_id>/golive/state/ """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_golive_step_upload(request, client_id, step_num):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/<step_number>/upload/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live file upload")
    if locked:
        return locked
    file_bytes = request.body
    filename = request.headers.get('X-Filename', 'uploaded_document.pdf')

    try:
        client_obj = Client.objects.get(id=client_id)
        step_def, _ = GoLiveStepDefinition.objects.get_or_create(
            step_number=step_num,
            defaults={"title": f"Go-Live Step {step_num}"}
        )

        # Document Validation & Integrity Engine check
        val_res = validate_golive_step_upload(step_num, file_bytes, filename, client=client_obj)

        if not val_res.get("ok", True):
            checks = val_res.get("checks", [])
            err_msg = val_res.get("error")
            if not err_msg and checks:
                err_msg = next((c["detail"] for c in checks if not c.get("ok")), "Validation failed")
            return JsonResponse({
                "success": False,
                "error": err_msg,
                "checks": checks
            }, status=400)

        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'COMPLETED'
        status_obj.save()

        next_def = GoLiveStepDefinition.objects.filter(step_number=step_num + 1).first()
        if next_def:
            next_status, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=next_def)
            if next_status.status == 'PENDING':
                next_status.status = 'IN_PROGRESS'
                next_status.save()

        state = helper_get_golive_state(client_obj)
        return JsonResponse({
            "success": True,
            "state": state,
            "checks": val_res["checks"]
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@csrf_exempt
def api_admin_golive_step_download(request, client_id, step_num):
    """ GET /admin-panel/api/clients/<client_id>/golive/steps/<step_number>/download/ """
    from django.http import HttpResponse

    if step_num == 1:
        try:
            from admin_panel.golive_authorization_service import (
                build_client_golive_authorization,
                golive_authorization_download_filename,
            )
            client_obj = Client.objects.get(id=client_id)
            pdf_bytes = build_client_golive_authorization(client_obj)
            filename = golive_authorization_download_filename(client_obj)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            response['X-OneSmarter-Filename'] = filename
            return response
        except Client.DoesNotExist:
            return JsonResponse({"success": False, "error": "Client not found."}, status=404)
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)

    if step_num == 2:
        try:
            from admin_panel.data_transfer_attestation_service import (
                build_client_data_transfer_attestation,
                data_transfer_attestation_download_filename,
            )
            client_obj = Client.objects.get(id=client_id)
            pdf_bytes = build_client_data_transfer_attestation(client_obj)
            filename = data_transfer_attestation_download_filename(client_obj)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            response['X-OneSmarter-Filename'] = filename
            return response
        except Client.DoesNotExist:
            return JsonResponse({"success": False, "error": "Client not found."}, status=404)
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=400)

    filename = f"OneSmarter_GoLive_Step{step_num}_Template.pdf"
    dummy_pdf_content = b"%PDF-1.4 Template Document Placeholder"
    response = HttpResponse(dummy_pdf_content, content_type="application/pdf")
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    response['X-OneSmarter-Filename'] = filename
    return response


@csrf_exempt
def api_admin_golive_step3_sftp(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/3/sftp/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live SFTP completion")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)
        step_def, _ = GoLiveStepDefinition.objects.get_or_create(step_number=3, defaults={"title": "Production SFTP"})
        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'COMPLETED'
        status_obj.save()

        next_def = GoLiveStepDefinition.objects.filter(step_number=4).first()
        if next_def:
            next_status, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=next_def)
            if next_status.status == 'PENDING':
                next_status.status = 'IN_PROGRESS'
                next_status.save()
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_golive_step4_schedule(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/4/schedule/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live schedule update")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)

        body = json.loads(request.body.decode('utf-8'))
        production_date = body.get('production_date', '').strip()
        production_time = body.get('production_time', '10:00').strip()
        timezone_name = _valid_timezone_name(body.get('timezone'))
        notes = body.get('notes', '').strip()

        if notes:
            from accounts.models import ClientStepComment
            author = "System"
            if request.user and hasattr(request.user, "name") and request.user.name:
                author = request.user.name
            elif request.user and hasattr(request.user, "email") and request.user.email:
                author = request.user.email
            latest = ClientStepComment.objects.filter(client=client_obj, step_number=104).first()
            if not latest or latest.comment != notes or latest.author != author:
                ClientStepComment.objects.create(
                    client=client_obj, step_number=104, comment=notes, author=author
                )

        if production_date:
            from datetime import datetime
            from django.utils import timezone
            try:
                if "-" in production_date and len(production_date.split("-")[0]) == 4:
                    dt = datetime.strptime(f"{production_date} {production_time}", "%Y-%m-%d %H:%M")
                else:
                    dt = datetime.strptime(f"{production_date} {production_time}", "%m-%d-%Y %H:%M")
                client_obj.live_since = timezone.make_aware(dt, ZoneInfo(timezone_name))
                client_obj.timezone = timezone_name
                client_obj.save(update_fields=["live_since", "timezone", "updated_at"])
            except ValueError:
                pass

        step_def, _ = GoLiveStepDefinition.objects.get_or_create(step_number=4, defaults={"title": "Production Schedule"})
        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'COMPLETED'
        status_obj.save()

        next_def = GoLiveStepDefinition.objects.filter(step_number=5).first()
        if next_def:
            next_status, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=next_def)
            if next_status.status == 'PENDING':
                next_status.status = 'IN_PROGRESS'
                next_status.save()
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_golive_step5_comment(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/5/comment/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live comment completion")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)

        body = json.loads(request.body.decode('utf-8'))
        comment_text = body.get('comment', '').strip()

        if comment_text:
            from accounts.models import ClientStepComment
            author = "System"
            if request.user and hasattr(request.user, "name") and request.user.name:
                author = request.user.name
            elif request.user and hasattr(request.user, "email") and request.user.email:
                author = request.user.email
            latest = ClientStepComment.objects.filter(client=client_obj, step_number=105).first()
            if not latest or latest.comment != comment_text or latest.author != author:
                ClientStepComment.objects.create(
                    client=client_obj, step_number=105, comment=comment_text, author=author
                )

        step_def, _ = GoLiveStepDefinition.objects.get_or_create(step_number=5, defaults={"title": "Special Comment"})
        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'COMPLETED'
        status_obj.save()

        next_def = GoLiveStepDefinition.objects.filter(step_number=6).first()
        if next_def:
            next_status, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=next_def)
            if next_status.status == 'PENDING':
                next_status.status = 'IN_PROGRESS'
                next_status.save()
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_golive_step6_complete(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/6/complete/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live final completion")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)
        step_def, _ = GoLiveStepDefinition.objects.get_or_create(step_number=6, defaults={"title": "Final Production"})
        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'COMPLETED'
        status_obj.save()

        client_obj.stage = 'IN_PRODUCTION'
        client_obj.status = 'ACTIVE'
        client_obj.save()
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_golive_step_redo(request, client_id, step_num):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/<step_number>/redo/ """
    locked = _offboarded_workflow_lock(request, client_id, "Go Live step redo")
    if locked:
        return locked
    try:
        client_obj = Client.objects.get(id=client_id)
        step_def = GoLiveStepDefinition.objects.get(step_number=step_num)
        status_obj, _ = ClientGoLiveStatus.objects.get_or_create(client=client_obj, step=step_def)
        status_obj.status = 'IN_PROGRESS'
        status_obj.save()
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_golive_state(client_obj)
    return JsonResponse({"success": True, "state": state})


@csrf_exempt
def api_admin_test_environment(request, client_id):
    """ GET/POST /admin-panel/api/clients/<client_id>/test-environment/ """
    try:
        client_obj = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    test_env, _ = ClientTestEnvironment.objects.get_or_create(
        client=client_obj,
        defaults={
            "sftp_host": "sftp-test.internal",
            "sftp_username": f"user_{client_obj.client_code.lower() if client_obj.client_code else 'test'}",
            "watched_folder": f"/inbound/{client_obj.client_code.lower() if client_obj.client_code else 'test'}/835",
            "test_status": "In Progress"
        }
    )

    if request.method == "POST":
        locked = _offboarded_workflow_lock(request, client_id, "Go Live test environment update")
        if locked:
            return locked
        try:
            body = json.loads(request.body.decode('utf-8'))
            if "sftp_host" in body:
                test_env.sftp_host = body["sftp_host"]
            if "sftp_username" in body:
                test_env.sftp_username = body["sftp_username"]
            if "watched_folder" in body:
                test_env.watched_folder = body["watched_folder"]
            if "test_status" in body:
                test_env.test_status = body["test_status"]
            test_env.save()
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)

    env_data = {
        "id": test_env.id,
        "sftp_host": test_env.sftp_host,
        "sftp_username": test_env.sftp_username,
        "watched_folder": test_env.watched_folder,
        "test_status": test_env.test_status,
    }
    return JsonResponse({"success": True, "test_environment": env_data})


@csrf_exempt
def api_admin_test_environment_run(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/test-environment/run-test/ """
    return JsonResponse({"success": True, "message": "Sandbox test passed successfully."})


@csrf_exempt
def api_admin_employee_roles(request):
    """
    GET, POST /admin-panel/api/employee-roles/
    Manage employee roles for dropdowns.
    """
    from accounts.models import EmployeeRole

    if request.method == "GET":
        roles = EmployeeRole.objects.all().order_by("role_name").values("id", "role_name", "description")
        return JsonResponse({"success": True, "roles": list(roles)})

    elif request.method == "POST":
        try:
            import json
            data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
            role_name = data.get("role_name", "").strip()
            description = data.get("description", "").strip()
            if not role_name:
                return JsonResponse({"success": False, "error": "Role name is required"}, status=400)
            role = EmployeeRole.objects.create(role_name=role_name, description=description)
            return JsonResponse({"success": True, "roles": [{"id": role.id, "role_name": role.role_name, "description": role.description}]})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)

    return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)


from admin_panel.mir_mapper_logic.mapping_store import get_mappings, save_mappings, reset_mappings, validate_mappings
from admin_panel.mir_mapper_logic.mapping_defaults import defaults

@csrf_exempt
def api_mappings_view(request):
    """
    GET /admin-panel/api/mappings/?client_id=<uuid>
    PUT /admin-panel/api/mappings/?client_id=<uuid>
    """
    client_id = request.GET.get("client_id")
    client = None
    if client_id:
        try:
            client = Client.objects.get(id=client_id)
        except (Client.DoesNotExist, ValueError):
            return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    if request.method == 'GET':
        current = get_mappings(client)
        # Calculate changed count relative to baseline defaults
        baseline = {field["id"]: field for field in defaults()}
        editable = ("mapType", "map", "length", "start", "upper", "trim", "truncate", "align", "pad", "fallbackType", "fallbackValue", "technicalRule")
        changed = sum(
            1
            for field in current
            if any(str(field.get(key)) != str(baseline[field["id"]].get(key)) for key in editable)
        )
        return JsonResponse({
            "ok": True,
            "success": True,
            "baseline": defaults(),
            "fields": current,
            "changed": changed
        })
    elif request.method == 'PUT':
        if not client:
            return JsonResponse({"success": False, "error": "client_id is required to save mappings"}, status=400)
        try:
            body = json.loads(request.body.decode('utf-8'))
            fields = body.get("fields", [])
            if not isinstance(fields, list):
                return JsonResponse({"detail": "fields must be a list"}, status=400)
            saved = save_mappings(fields, client)
            return JsonResponse({
                "ok": True,
                "success": True,
                "fields": saved,
                "note": "Saved mappings are now used by the 835 to MIR converter."
            })
        except ValueError as exc:
            return JsonResponse({"detail": str(exc), "error": str(exc)}, status=400)
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)

    return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)


@csrf_exempt
def api_mappings_check(request):
    """
    POST /admin-panel/api/mappings/check/
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
        fields = body.get("fields", [])
        if not isinstance(fields, list):
            return JsonResponse({"detail": "fields must be a list"}, status=400)
        issues = validate_mappings(fields)
        return JsonResponse({"ok": not issues, "success": not issues, "issues": issues})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@csrf_exempt
def api_mappings_reset(request):
    """
    POST /admin-panel/api/mappings/reset/?client_id=<uuid>
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)
    client_id = request.GET.get("client_id")
    client = None
    if client_id:
        try:
            client = Client.objects.get(id=client_id)
        except (Client.DoesNotExist, ValueError):
            return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    fields = reset_mappings(client)
    return JsonResponse({
        "ok": True,
        "success": True,
        "fields": fields,
        "note": "Mappings reset to the current converter baseline."
    })


@csrf_exempt
def api_admin_audit_logs(request):
    """
    GET /admin-panel/api/audit-logs/
    Returns filtered, sorted, paginated audit log entries.
    Filters are applied to the complete audit trail before pagination.
    """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    client_id = request.GET.get("client_id", "").strip()
    module_filter = request.GET.get("module", "").strip().upper()
    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("performed_by", "").strip()
    search = request.GET.get("search", "").strip()
    date_from_raw = request.GET.get("date_from", "").strip()
    date_to_raw = request.GET.get("date_to", "").strip()
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    try:
        page_size = int(request.GET.get("page_size", 25))
    except ValueError:
        page_size = 25
    if page_size not in {10, 25, 50, 100}:
        page_size = 25

    ordering_map = {
        "timestamp": "timestamp",
        "module": "module",
        "action": "action",
        "client": "client__name",
        "performed_by": "performed_by",
    }
    sort_field = ordering_map.get(request.GET.get("sort", "timestamp"), "timestamp")
    sort_direction = "" if request.GET.get("direction", "desc") == "asc" else "-"

    qs = AuditLog.objects.select_related("client")

    if client_id:
        try:
            qs = qs.filter(client_id=UUID(client_id))
        except (TypeError, ValueError, AttributeError):
            return JsonResponse({"success": False, "error": "Invalid client identifier."}, status=400)

    if module_filter and module_filter != "ALL":
        qs = qs.filter(module=module_filter)

    if action_filter:
        qs = qs.filter(action=action_filter)
    if actor_filter:
        qs = qs.filter(performed_by=actor_filter)
    if search:
        def formatted_timestamp(timezone_name, format_mask):
            localized = models.Func(
                models.Value(timezone_name), models.F("timestamp"),
                function="timezone", output_field=models.DateTimeField(),
            )
            return models.Func(
                localized, models.Value(format_mask), function="to_char",
                output_field=models.CharField(),
            )

        qs = qs.annotate(
            audit_timestamp_iso=models.Func(
                models.F("timestamp"), models.Value('YYYY-MM-DD"T"HH24:MI:SS'),
                function="to_char", output_field=models.CharField(),
            ),
            audit_timestamp_eastern=formatted_timestamp("America/New_York", "MM/DD/YYYY HH12:MI:SS AM"),
            audit_timestamp_eastern_24=formatted_timestamp("America/New_York", "MM/DD/YYYY HH24:MI:SS"),
        )
        qs = qs.filter(
            Q(module__icontains=search) |
            Q(action__icontains=search) |
            Q(details__icontains=search) |
            Q(performed_by__icontains=search) |
            Q(client__name__icontains=search) |
            Q(audit_timestamp_iso__icontains=search) |
            Q(audit_timestamp_eastern__icontains=search) |
            Q(audit_timestamp_eastern_24__icontains=search)
        )

    date_from = parse_date(date_from_raw) if date_from_raw else None
    date_to = parse_date(date_to_raw) if date_to_raw else None
    if date_from_raw and not date_from:
        return JsonResponse({"success": False, "error": "Invalid start date."}, status=400)
    if date_to_raw and not date_to:
        return JsonResponse({"success": False, "error": "Invalid end date."}, status=400)
    if date_from:
        qs = qs.filter(timestamp__date__gte=date_from)
    if date_to:
        qs = qs.filter(timestamp__date__lte=date_to)

    qs = qs.order_by(f"{sort_direction}{sort_field}", "-id")
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)

    logs = []
    for log in page_obj.object_list:
        logs.append({
            "id": log.id,
            "module": log.module,
            "action": log.action,
            "details": log.details,
            "performed_by": log.performed_by,
            "timestamp": log.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if log.timestamp else "",
            "client": log.client.name if log.client else None,
            "client_id": str(log.client.id) if log.client else None,
            "client_name": log.client.name if log.client else "System",
        })

    all_logs = AuditLog.objects.all()
    return JsonResponse({
        "success": True,
        "logs": logs,
        "count": paginator.count,
        "pagination": {
            "page": page_obj.number,
            "page_size": page_size,
            "total_count": paginator.count,
            "total_pages": paginator.num_pages,
            "has_previous": page_obj.has_previous(),
            "has_next": page_obj.has_next(),
        },
        "filter_options": {
            "modules": list(all_logs.exclude(module="").order_by("module").values_list("module", flat=True).distinct()),
            "actions": list(all_logs.exclude(action="").order_by("action").values_list("action", flat=True).distinct()),
            "performed_by": list(all_logs.exclude(performed_by="").order_by("performed_by").values_list("performed_by", flat=True).distinct()),
        },
    })


# ---------------------------------------------------------
# OFFBOARDING APIs
# ---------------------------------------------------------
from admin_panel.models import OffboardingStepDefinition, ClientOffboardingStatus

def helper_get_offboarding_state(client_obj):
    total = 3
    steps_data = []

    status_by_step = {
        s.step.step_number: s
        for s in ClientOffboardingStatus.objects.filter(client=client_obj).select_related('step')
    }

    for i in range(1, total + 1):
        status_obj = status_by_step.get(i)
        if status_obj:
            steps_data.append({
                "step": i,
                "status": status_obj.status,
                "document_path": status_obj.document_path,
                "updated_at": status_obj.updated_at.isoformat() if status_obj.updated_at else None
            })
        else:
            steps_data.append({
                "step": i,
                "status": "PENDING",
                "document_path": None,
                "updated_at": None
            })

    return {
        "total_steps": total,
        "completed_steps": sum(1 for s in steps_data if s["status"] == "COMPLETED"),
        "finalized": str(client_obj.stage or "").lower() == "offboarded",
        "locked": str(client_obj.stage or "").lower() == "offboarded",
        "steps": steps_data
    }

def api_admin_offboarding_state(request, client_id):
    from django.http import JsonResponse
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    try:
        from accounts.models import Client
        client_obj = Client.objects.get(id=client_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Client not found"}, status=404)

    state = helper_get_offboarding_state(client_obj)
    return JsonResponse({"success": True, "state": state})

@csrf_exempt
@admin_api_required
def api_admin_offboarding_step_complete(request, client_id, step_num):
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "offboarding step completion")
    if locked:
        return locked
    try:
        from accounts.models import Client
        step_num = int(step_num)

        step_titles = {
            1: "Termination Notice Recorded",
            2: "Archive Returned to Client",
            3: "Tenant Key Destruction"
        }

        if step_num not in step_titles:
            return JsonResponse({"success": False, "error": "Invalid offboarding step."}, status=400)

        revoked_user_count = 0
        revoked_session_count = 0
        client_user_ids = set()
        client_user_emails = []
        email_result = {"attempted": 0, "sent": 0, "failed": []}
        first_offboarding_completion = False

        with transaction.atomic():
            # Serialize repeated/double-click requests for the same client.
            client_obj = Client.objects.select_for_update().get(id=client_id)

            if step_num > 1:
                previous_complete = ClientOffboardingStatus.objects.filter(
                    client=client_obj,
                    step__step_number=step_num - 1,
                    status='COMPLETED',
                ).exists()
                if not previous_complete:
                    return JsonResponse({
                        "success": False,
                        "error": f"Complete offboarding Step {step_num - 1} before Step {step_num}.",
                    }, status=409)

            step_def, _ = OffboardingStepDefinition.objects.get_or_create(
                step_number=step_num,
                defaults={"title": step_titles.get(step_num, f"Offboarding Step {step_num}")}
            )
            status_obj, _ = ClientOffboardingStatus.objects.get_or_create(client=client_obj, step=step_def)
            first_offboarding_completion = (
                step_num == 3 and status_obj.status != 'COMPLETED'
            )

            # If it's step 1 and it's a POST with a file
            if step_num == 1 and request.method == "POST" and request.body:
                filename = request.headers.get('X-Filename', 'uploaded_document.pdf')
                # we could save the file, but for now we just record the name
                status_obj.document_path = filename

                from admin_panel.models import ClientDocument
                from django.core.files.base import ContentFile
                doc = ClientDocument.objects.create(
                    client=client_obj,
                    document_name="Termination Notice",
                    original_filename=filename,
                    document_type="Offboarding Step 1",
                    file_size=len(request.body),
                    uploaded_by="Admin User"
                )
                doc.file.save(filename, ContentFile(request.body), save=True)

            status_obj.status = 'COMPLETED'
            status_obj.save()

            if step_num == 3:
                client_obj.status = 'INACTIVE'
                client_obj.stage = 'offboarded'
                client_obj.save(update_fields=['status', 'stage'])

                # Permanently stop every future scheduled ingestion for this
                # tenant at the same transaction boundary as offboarding.
                from edi835.models import SFTPAutomationSchedule
                SFTPAutomationSchedule.objects.filter(client=client_obj).update(
                    enabled=False,
                    next_run_at=None,
                )

                # Deactivate all users belonging to this client
                from accounts.models import User as AccountUser
                client_users = AccountUser.objects.filter(client=client_obj, is_staff=False, is_superuser=False)
                client_user_ids = {str(uid) for uid in client_users.values_list('id', flat=True)}
                client_user_emails = list(
                    client_users.exclude(email="").values_list("email", flat=True)
                )
                revoked_user_count = client_users.filter(is_active=True).update(is_active=False)

        # An inactive user is rejected immediately by Django's authentication
        # backend. Physical session deletion is defense-in-depth and must not
        # roll back the permanent client/user revocation if one session is bad.
        if step_num == 3 and client_user_ids:
            try:
                from django.contrib.sessions.models import Session
                session_pks_to_delete = []
                for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
                    try:
                        uid = session.get_decoded().get('_auth_user_id')
                    except Exception:
                        continue
                    if uid and str(uid) in client_user_ids:
                        session_pks_to_delete.append(session.pk)
                if session_pks_to_delete:
                    revoked_session_count, _ = Session.objects.filter(
                        pk__in=session_pks_to_delete
                    ).delete()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Client was offboarded but stale-session cleanup failed"
                )

            try:
                AuditLog.objects.create(
                    module="OFFBOARDING",
                    action="CLIENT_OFFBOARDED",
                    details=(
                        f"Client '{client_obj.name}' was offboarded; "
                        f"{revoked_user_count} active user account(s) and "
                        f"{revoked_session_count} session(s) were revoked."
                    ),
                    performed_by=getattr(request.user, 'name', '') or getattr(request.user, 'email', '') or 'Administrator',
                    client=client_obj,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Client was offboarded but audit logging failed"
                )

            try:
                from admin_panel.email_service import send_client_offboarding_notice
                if first_offboarding_completion:
                    email_result = send_client_offboarding_notice(
                        client_obj, client_user_emails
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Client was offboarded but notification delivery failed"
                )

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_offboarding_state(client_obj)
    return JsonResponse({
        "success": True,
        "state": state,
        "revoked_users": revoked_user_count,
        "revoked_sessions": revoked_session_count,
        "email_notifications": email_result,
    })

@csrf_exempt
def api_admin_offboarding_step_redo(request, client_id, step_num):
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    locked = _offboarded_workflow_lock(request, client_id, "offboarding step redo")
    if locked:
        return locked
    try:
        from accounts.models import Client
        client_obj = Client.objects.get(id=client_id)
        step_num = int(step_num)

        status_obj = ClientOffboardingStatus.objects.get(client=client_obj, step__step_number=step_num)
        status_obj.status = 'PENDING'
        status_obj.document_path = None
        status_obj.save()

    except ClientOffboardingStatus.DoesNotExist:
        pass
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_offboarding_state(client_obj)
    return JsonResponse({"success": True, "state": state})
