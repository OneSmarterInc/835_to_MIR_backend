from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from admin_panel.models import log_audit_event
from project835.decorators import admin_api_required

from .models import User


@csrf_exempt
@admin_api_required
def api_account_security(request, user_id):
    target = User.objects.select_related("client").filter(id=user_id).first()
    if target is None:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)

    if request.method == "GET":
        return JsonResponse({
            "success": True,
            "security": {
                "user_id": target.id,
                "email": target.email,
                "blocked": bool(target.login_blocked_at),
                "failed_login_attempts": int(target.failed_login_attempts or 0),
                "blocked_at": target.login_blocked_at.isoformat() if target.login_blocked_at else None,
                "reason": target.login_blocked_reason or "",
                "metadata": target.login_blocked_metadata or {},
            },
        })

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only GET and POST are allowed."}, status=405)

    if (target.is_staff or target.is_superuser) and not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Only a Super Admin can unblock an administrator account."}, status=403)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        data = {}
    action = str(data.get("action") or "unblock").strip().lower()
    if action != "unblock":
        return JsonResponse({"success": False, "error": "Unsupported security action."}, status=400)

    was_blocked = bool(target.login_blocked_at)
    target.failed_login_attempts = 0
    target.login_blocked_at = None
    target.login_blocked_reason = ""
    target.login_blocked_metadata = {}
    target.save(update_fields=[
        "failed_login_attempts", "login_blocked_at", "login_blocked_reason",
        "login_blocked_metadata", "updated_at",
    ])

    try:
        log_audit_event(
            module="AUTH",
            action="ACCOUNT_UNBLOCKED",
            details=f"User '{target.email}' was unblocked after password-attempt lockout.",
            performed_by=getattr(request.user, "name", "") or request.user.email,
            client=target.client,
        )
    except Exception:
        pass

    return JsonResponse({
        "success": True,
        "message": "User account has been unblocked." if was_blocked else "User account was already unblocked.",
        "security": {
            "user_id": target.id,
            "email": target.email,
            "blocked": False,
            "failed_login_attempts": 0,
            "blocked_at": None,
            "reason": "",
            "metadata": {},
        },
    })
