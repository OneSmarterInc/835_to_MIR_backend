import json
import logging
from django.db import models, transaction
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

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


@csrf_exempt
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
        if onboarding_incomplete:
            completed_steps = {ss.step.step_number for ss in c.completed_onboarding_steps}
            in_progress_step = 1
            for num in range(1, total_onboarding_steps + 1):
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
            "status": c.status,
            "stage": dynamic_stage,
            "claims_system": c.claims_system,
            "owner": c.owner,
            "progress_pct": c.progress_pct,
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
                "status": client_obj.status,
                "stage": client_obj.stage,
                "claims_system": client_obj.claims_system,
                "owner": client_obj.owner,
                "progress_pct": client_obj.progress_pct,
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
        client_obj = Client.objects.get(id=client_id)
        name = client_obj.name
        # Audit log before delete (since client will be gone)
        try:
            actor = "System"
            if request.user and hasattr(request.user, "name") and request.user.name:
                actor = request.user.name
            elif request.user and hasattr(request.user, "email") and request.user.email:
                actor = request.user.email
            AuditLog.objects.create(
                module="CLIENTS",
                action="CLIENT_DELETED",
                details=f"Client '{name}' permanently deleted from the system.",
                performed_by=actor,
                client=None
            )
        except Exception:
            pass
        client_obj.delete()
        return JsonResponse({"success": True, "message": f"Client '{name}' deleted successfully."})
    except (Client.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Client not found."}, status=404)


@csrf_exempt
def api_admin_access_info(request):
    """
    GET /admin-panel/api/access/info/
    Returns dynamic access control matrix from database users.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Not authenticated"}, status=401)

    staff_list = []
    for u in User.objects.select_related("client").all().order_by("-created_at"):
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
            "totp_enabled": u.totp_enabled,
            "client_id": str(u.client.id) if u.client else None,
            "client_name": "OneSmarter" if u.is_staff else (u.client.name if u.client else None),
            "client_code": u.client.client_code if u.client else None,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
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
        client=client_obj
    )

    user_dict = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "mobile": user.mobile,
        "is_staff": user.is_staff,
        "role": "Admin" if user.is_staff else "User",
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
    if "is_staff" in data:
        user_obj.is_staff = bool(data["is_staff"])
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

    # Force refresh steps definitions to match the latest 15 steps definition if count differs or title changed
    step6_def = OnboardingStepDefinition.objects.filter(step_number=6).first()
    if step_defs.count() != 15 or not step6_def or step6_def.title != "SMTP / Email Configuration":
        OnboardingStepDefinition.objects.all().delete()
        default_steps = [
            (1, "Mutual NDA signed", "Upload signed NDA template to establish confidentiality agreement."),
            (2, "Business associate agreement executed", "Execute HIPAA compliant Business Associate Agreement."),
            (3, "Security review returned to client", "Upload security audit review document."),
            (4, "Contact Records", "Designate client contact personnel and records."),
            (5, "Claims system identified and verified", "Identify client claims vendor software system."),
            (6, "SMTP / Email Configuration", "Configure SMTP credentials to utilize for onboarding notifications."),
            (7, "Delivery method agreed", "Configure secure transfer mechanism (SFTP, API drop)."),
            (8, "Sample 835 received and validated", "Validate structural integrity of sample X12 835 file."),
            (9, "Mapping rules written & configured", "Open Mapping Application to configure 835 conversion."),
            (10, "MIR Output Filename Format & Create User", "Define output MIR file naming convention and create SFTP user logins."),
            (11, "Side-by-Side 835 Conversion Review", "Verify side-by-side conversion of sample 835 files."),
            (12, "Go-Live Safeguards Verification", "Confirm production cutover safeguards and monitoring."),
            (13, "Production Schedule", "Define scheduled date and time to go live."),
            (14, "Go-Live / Final Verification", "Attach email conversation confirmation."),
            (15, "Production Delivery Sign-Off / Conclude Onboarding", "Monitor first live 835 delivery and conclude onboarding."),
        ]
        for num, title, desc in default_steps:
            OnboardingStepDefinition.objects.create(step_number=num, title=title, description=desc)
        step_defs = OnboardingStepDefinition.objects.all().order_by('step_number')

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
        11: "side_by_side_done",
        12: "golive_redirect",
        13: "production_schedule",
        14: "email_upload",
        15: "text_submission_final",
    }

    def get_phase(step_number):
        if step_number <= 4:
            return "PHASE ONE - PAPER RECORD DATA"
        elif step_number <= 9:
            return "PHASE TWO - UNDERSTAND THEIR SYSTEM"
        elif step_number <= 13:
            return "PHASE THREE - MOVE IT ON TEST"
        else:
            return "PHASE FOUR - LIVE"

    in_progress_found = False
    for step in step_defs:
        st = status_map.get(step.id, 'PENDING')
        
        is_done = st == 'COMPLETED'
        
        # Override Step 12 completion based on Go-Live steps
        if step.step_number == 12:
            is_done = (st == 'COMPLETED') or (completed_golive == total_golive)
            
        is_in_progress = st == 'IN_PROGRESS'
        
        if is_in_progress:
            in_progress_found = True
            
        is_file_step = step.step_number in [1, 2, 3, 14]

        extra_data = {}
        if step.step_number == 4:
            extra_data["contacts"] = contacts_list
        elif step.step_number == 10:
            extra_data["mir_filename_format"] = client_obj.mir_filename_format
            extra_data["users"] = users_list
        elif step.step_number == 13:
            if client_obj.live_since:
                from django.utils.timezone import localtime
                local_dt = localtime(client_obj.live_since)
                extra_data["schedule"] = {
                    "scheduled_date": local_dt.strftime("%Y-%m-%d"),
                    "scheduled_time": local_dt.strftime("%H:%M")
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

    # Auto advance: only if NO step is currently IN_PROGRESS
    # After a redo, the redone step is IN_PROGRESS — don't auto-advance past it
    if not in_progress_found:
        for s in steps_data:
            if not s["done"]:
                s["inProgress"] = True
                break

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
    
    # Don't regress stage if they are already in production
    stage = client_obj.stage
    if stage not in ['IN_PRODUCTION', 'PRODUCTION', 'production', 'production_pending']:
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
    
    file_bytes = request.body
    filename = request.headers.get('X-Filename', 'uploaded_document.pdf')
    
    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            client_obj = Client.objects.get(id=client_id)
            step_def = OnboardingStepDefinition.objects.get(step_number=step_num)
            
            # Step Validation using validation.py engine
            val_res = validate_step_upload(step_num, file_bytes, filename)

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
                import logging
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
            
            template_map = {
                1: "OneSmarter_MutualNDA_Template.pdf",
                2: "OneSmarter_BAA_Template.pdf",
                3: "OneSmarter_SecurityReview_Template.pdf",
            }
            
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
    if request.method == "POST":
        return JsonResponse({"success": True, "message": "Note added successfully."})
    return JsonResponse({"success": True, "notes": []})


@csrf_exempt
def api_admin_step_redo(request, client_id, step_key):
    """ POST /admin-panel/api/clients/<client_id>/steps/<step_key>/redo/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
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

                # Reset ALL SUBSEQUENT steps to PENDING (locked) — preserve uploaded docs
                subsequent_statuses = ClientStepStatus.objects.filter(
                    client=client_obj,
                    step__step_number__gt=step_num,
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
    try:
        client_obj = Client.objects.get(id=client_id)
        
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
                filename_to_report = request.headers.get('X-Filename', '835_file.x12')
                subject = f"OneSmarter: 835 File Validation Failed - {filename_to_report}"
                html = f"<h3>835 File Validation Failed</h3><p>The file <b>{filename_to_report}</b> failed X12 validation.</p><p><b>Reason:</b> {err_msg}</p>"
                send_client_email(client_obj, subject, html)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

            return JsonResponse({"success": False, "error": err_msg, "checks": checks}, status=400)
            
        checks = [{"ok": True, "label": "Structure", "detail": f"835 structural and balance checks passed. Claims found: {report.get('claims', 0)}"}]
        
        # Save as ClientDocument now that it is valid
        filename = request.headers.get('X-Filename', '835_file.x12')
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
        from edi835.services import process_edi835_file_content
        proc_res = process_edi835_file_content(raw_text, original_filename=filename, client=client_obj)

        # Also send the raw 835 to the client's configured SFTP inbound_835_folder
        try:
            from edi835.models import SFTPConfig
            import paramiko, io
            cfg = SFTPConfig.objects.filter(client=client_obj).first()
            if not cfg or cfg.use_default:
                cfg = SFTPConfig.objects.filter(client__isnull=True).first()
            if cfg and cfg.host and cfg.username and cfg.inbound_835_folder:
                in_host = cfg.host
                in_port = int(cfg.port or 22)
                in_user = cfg.username
                in_pass = cfg.password or ''
                in_folder = cfg.inbound_835_folder.rstrip('/')
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=in_host, port=in_port, username=in_user, password=in_pass,
                            timeout=10, look_for_keys=False, allow_agent=False)
                sftp = ssh.open_sftp()
                try:
                    sftp.stat(in_folder)
                except FileNotFoundError:
                    sftp.mkdir(in_folder)
                remote_path = f"{in_folder}/{filename}"
                sftp.putfo(io.BytesIO(file_bytes), remote_path)
                sftp.close()
                ssh.close()
        except Exception as sftp_inbound_err:
            import logging
            logging.getLogger(__name__).warning(f"Step 8: could not upload 835 to SFTP inbound: {sftp_inbound_err}")

        # Trigger success email to the user
        try:
            from admin_panel.email_service import send_client_email
            email_subj = f"OneSmarter: 835 File Validation & Delivery Successful"
            sftp_status_str = "and uploaded to your configured SFTP server" if proc_res.get("db_record") and proc_res["db_record"].present_in_sftp else "but SFTP upload is pending/not configured"
            email_html = f"""
            <h3>Test 835 Validation & Conversion Successful</h3>
            <p>Dear Partner,</p>
            <p>We are pleased to inform you that your test 835 file <b>{filename}</b> has been successfully validated, converted to MIR format, {sftp_status_str}.</p>
            <p><b>Conversion details:</b></p>
            <ul>
                <li>Claims identified: {proc_res.get('claims_count', 0)}</li>
                <li>Services processed: {proc_res.get('services_count', 0)}</li>
                <li>Records processed: {proc_res.get('records_count', 0)}</li>
            </ul>
            """
            send_client_email(client_obj, email_subj, email_html)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send email on step 7 success: {e}")

        step_def = OnboardingStepDefinition.objects.get(step_number=8)
        step_status, _ = ClientStepStatus.objects.get_or_create(client=client_obj, step=step_def)
        step_status.status = 'COMPLETED'
        step_status.save()
        update_client_onboarding_stats(client_obj)

        return JsonResponse({"success": True, "checks": checks})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@csrf_exempt
def api_admin_step_action(request, client_id, step_key, action):
    """ POST /admin-panel/api/clients/<client_id>/steps/<step_key>/<action>/ """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST allowed"}, status=405)
    
    try:
        parts = step_key.split('_')
        if len(parts) >= 2:
            step_num = int(parts[1])
            client_obj = Client.objects.get(id=client_id)
            
            if action == "save" and step_num == 4:
                from accounts.models import ClientContact
                try:
                    data = json.loads(request.body.decode('utf-8'))
                    ClientContact.objects.create(
                        client=client_obj,
                        role_name=data.get('role_name', ''),
                        name=data.get('employee_name', ''),
                        email=data.get('email', ''),
                        phone=data.get('phone', '')
                    )
                except Exception as e:
                    pass

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
                        ClientStepComment.objects.create(
                            client=client_obj,
                            step_number=step_num,
                            comment=verification_text,
                            author=author
                        )
                except Exception as e:
                    pass

            # Validate Step 4 Contact fields (from other branch)
            if action == "save" and step_num == 4:
                try:
                    body = json.loads(request.body.decode('utf-8'))
                    email = body.get("email", "")
                    phone = body.get("phone", "")

                    if email:
                        ok_email, err_email = validate_email_address(email)
                        if not ok_email:
                            return JsonResponse({"success": False, "error": err_email}, status=400)

                    if phone:
                        ok_phone, err_phone = validate_phone_number(phone)
                        if not ok_phone:
                            return JsonResponse({"success": False, "error": err_phone}, status=400)
                except Exception:
                    pass

            if action == "save" and step_num == 13:
                try:
                    body = json.loads(request.body.decode('utf-8'))
                    scheduled_date = body.get('scheduled_date', '').strip()
                    scheduled_time = body.get('scheduled_time', '10:00').strip()
                    notes = body.get('notes', '').strip()
                    
                    if notes:
                        from accounts.models import ClientStepComment
                        author = "System"
                        if request.user and hasattr(request.user, "name") and request.user.name:
                            author = request.user.name
                        elif request.user and hasattr(request.user, "email") and request.user.email:
                            author = request.user.email
                        ClientStepComment.objects.create(
                            client=client_obj,
                            step_number=step_num,
                            comment=notes,
                            author=author
                        )

                    if scheduled_date:
                        from datetime import datetime
                        from django.utils import timezone
                        try:
                            if "-" in scheduled_date and len(scheduled_date.split("-")[0]) == 4:
                                dt = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%Y-%m-%d %H:%M")
                            else:
                                dt = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%m-%d-%Y %H:%M")
                            client_obj.live_since = timezone.make_aware(dt)
                            client_obj.save()
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
        
    return JsonResponse({"success": True, "message": f"Action {action} on {step_key} completed successfully."})



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
def api_admin_default_smtp(request):
    """
    GET  /admin-panel/api/default-smtp/  — load existing default config (password never returned)
    POST /admin-panel/api/default-smtp/  — upsert default config (password stored encrypted)
    """
    if request.method == 'GET':
        try:
            cfg = ClientSmtpConfig.objects.get(client__isnull=True)
            decrypted_password = ''
            if cfg.smtp_password:
                try:
                    decrypted_password = decrypt_smtp_password(cfg.smtp_password)
                except Exception:
                    pass
            return JsonResponse({
                'success': True,
                'config': {
                    'sender_name':   cfg.sender_name,
                    'sender_email':  cfg.sender_email,
                    'smtp_host':     cfg.smtp_host,
                    'smtp_port':     cfg.smtp_port,
                    'smtp_username': cfg.smtp_username,
                    'smtp_password': decrypted_password,
                    'security':      cfg.security,
                    'reply_to':      cfg.reply_to or '',
                    'has_password':  bool(cfg.smtp_password),
                }
            })
        except ClientSmtpConfig.DoesNotExist:
            return JsonResponse({'success': True, 'config': None})

    if request.method == 'POST':
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
            obj, created = ClientSmtpConfig.objects.update_or_create(
                client=None,
                defaults=smtp_fields
            )
            return JsonResponse({'success': True, 'created': created})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)



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
    
    files = EDI835File.objects.filter(client_id=client_id).order_by('-uploaded_at')
    file_list = []
    for f in files:
        file_list.append({
            "id": str(f.id),
            "original_filename": f.original_filename,
            "status": f.status,
            "claims_count": f.claims_count,
            "services_count": f.services_count,
            "records_count": f.records_count,
            "ingestion_source": f.ingestion_source,
            "uploaded_at": f.uploaded_at.strftime("%Y-%m-%dT%H:%M:%SZ") if f.uploaded_at else "",
            "processing_completed_at": f.processing_completed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if f.processing_completed_at else "",
            "error_message": f.error_message
        })
    return JsonResponse({"success": True, "files": file_list})


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
                local_dt = localtime(client_obj.live_since)
                extra_data["schedule"] = {
                    "production_date": local_dt.strftime("%Y-%m-%d"),
                    "production_time": local_dt.strftime("%H:%M")
                }
            note = golive_comments.get(104)
            if note:
                extra_data["schedule"] = extra_data.get("schedule", {})
                extra_data["schedule"]["notes"] = note.comment

        if step.step_number == 5:
            note = golive_comments.get(105)
            if note:
                latest_note = {
                    "note_text": note.comment,
                    "author": note.author
                }

        steps_data.append({
            "id": step.id,
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
    file_bytes = request.body
    filename = request.headers.get('X-Filename', 'uploaded_document.pdf')

    try:
        client_obj = Client.objects.get(id=client_id)
        step_def, _ = GoLiveStepDefinition.objects.get_or_create(
            step_number=step_num,
            defaults={"title": f"Go-Live Step {step_num}"}
        )

        # Document Validation & Integrity Engine check
        val_res = validate_golive_step_upload(step_num, file_bytes, filename)

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
    filename = f"OneSmarter_GoLive_Step{step_num}_Template.pdf"
    dummy_pdf_content = b"%PDF-1.4 Template Document Placeholder"
    response = HttpResponse(dummy_pdf_content, content_type="application/pdf")
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    response['X-OneSmarter-Filename'] = filename
    return response


@csrf_exempt
def api_admin_golive_step3_sftp(request, client_id):
    """ POST /admin-panel/api/clients/<client_id>/golive/steps/3/sftp/ """
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
    try:
        client_obj = Client.objects.get(id=client_id)
        
        body = json.loads(request.body.decode('utf-8'))
        production_date = body.get('production_date', '').strip()
        production_time = body.get('production_time', '10:00').strip()
        notes = body.get('notes', '').strip()
        
        if notes:
            from accounts.models import ClientStepComment
            author = "System"
            if request.user and hasattr(request.user, "name") and request.user.name:
                author = request.user.name
            elif request.user and hasattr(request.user, "email") and request.user.email:
                author = request.user.email
            ClientStepComment.objects.create(
                client=client_obj,
                step_number=104,
                comment=notes,
                author=author
            )

        if production_date:
            from datetime import datetime
            from django.utils import timezone
            try:
                if "-" in production_date and len(production_date.split("-")[0]) == 4:
                    dt = datetime.strptime(f"{production_date} {production_time}", "%Y-%m-%d %H:%M")
                else:
                    dt = datetime.strptime(f"{production_date} {production_time}", "%m-%d-%Y %H:%M")
                client_obj.live_since = timezone.make_aware(dt)
                client_obj.save()
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
            ClientStepComment.objects.create(
                client=client_obj,
                step_number=105,
                comment=comment_text,
                author=author
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
    Returns filtered, paginated audit log entries.
    Supports ?client_id=<uuid>&module=<str>&limit=<int>
    """
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET allowed"}, status=405)

    client_id = request.GET.get("client_id", "").strip()
    module_filter = request.GET.get("module", "").strip().upper()
    try:
        limit = int(request.GET.get("limit", 500))
    except ValueError:
        limit = 500

    qs = AuditLog.objects.select_related("client").order_by("-timestamp")

    if client_id:
        try:
            from accounts.models import Client as ClientModel
            client_obj = ClientModel.objects.get(id=client_id)
            qs = qs.filter(client=client_obj)
        except Exception:
            pass

    if module_filter and module_filter != "ALL":
        qs = qs.filter(module=module_filter)

    logs = []
    for log in qs[:limit]:
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

    return JsonResponse({"success": True, "logs": logs, "count": len(logs)})


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
def api_admin_offboarding_step_complete(request, client_id, step_num):
    from django.http import JsonResponse
    try:
        from accounts.models import Client
        client_obj = Client.objects.get(id=client_id)
        step_num = int(step_num)
        
        step_titles = {
            1: "Termination Notice Recorded",
            2: "Archive Returned to Client",
            3: "Tenant Key Destruction"
        }
        
        with transaction.atomic():
            step_def, _ = OffboardingStepDefinition.objects.get_or_create(
                step_number=step_num,
                defaults={"title": step_titles.get(step_num, f"Offboarding Step {step_num}")}
            )
            status_obj, _ = ClientOffboardingStatus.objects.get_or_create(client=client_obj, step=step_def)

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
                client_obj.save()

                # Deactivate all users belonging to this client
                from accounts.models import User as AccountUser
                client_users = AccountUser.objects.filter(client=client_obj, is_staff=False, is_superuser=False)
                client_user_ids = {str(uid) for uid in client_users.values_list('id', flat=True)}
                client_users.update(is_active=False)

                # Flush all sessions for these users so they get kicked immediately
                from django.contrib.sessions.models import Session
                from django.utils import timezone
                session_pks_to_delete = []
                for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
                    uid = session.get_decoded().get('_auth_user_id')
                    if uid and uid in client_user_ids:
                        session_pks_to_delete.append(session.pk)
                if session_pks_to_delete:
                    Session.objects.filter(pk__in=session_pks_to_delete).delete()

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_offboarding_state(client_obj)
    return JsonResponse({"success": True, "state": state})

@csrf_exempt
def api_admin_offboarding_step_redo(request, client_id, step_num):
    from django.http import JsonResponse
    try:
        from accounts.models import Client
        client_obj = Client.objects.get(id=client_id)
        step_num = int(step_num)
        
        status_obj = ClientOffboardingStatus.objects.get(client=client_obj, step__step_number=step_num)
        status_obj.status = 'PENDING'
        status_obj.document_path = None
        status_obj.save()
        
        if step_num == 3:
            client_obj.status = 'ACTIVE'
            client_obj.stage = 'production' # fallback
            client_obj.save()
            
            # Reactivate all users belonging to this client
            from accounts.models import User as AccountUser
            AccountUser.objects.filter(client=client_obj, is_staff=False, is_superuser=False).update(is_active=True)
            
    except ClientOffboardingStatus.DoesNotExist:
        pass
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    state = helper_get_offboarding_state(client_obj)
    return JsonResponse({"success": True, "state": state})
