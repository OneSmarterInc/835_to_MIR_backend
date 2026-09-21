from __future__ import annotations

import logging
from html import escape
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from .models import User


logger = logging.getLogger("accounts")
MAX_FAILED_PASSWORD_ATTEMPTS = 5
BLOCK_MESSAGE = "Account blocked. Contact an administrator."


def request_security_metadata(request) -> dict:
    if request is None:
        return {}
    meta = request.META
    forwarded = str(meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    ip = forwarded or str(meta.get("REMOTE_ADDR") or "").strip()
    city = str(meta.get("HTTP_X_VERCEL_IP_CITY") or meta.get("HTTP_CLOUDFRONT_VIEWER_CITY") or "").strip()
    region = str(meta.get("HTTP_X_VERCEL_IP_COUNTRY_REGION") or meta.get("HTTP_CLOUDFRONT_VIEWER_COUNTRY_REGION") or "").strip()
    country = str(meta.get("HTTP_X_VERCEL_IP_COUNTRY") or meta.get("HTTP_CLOUDFRONT_VIEWER_COUNTRY") or meta.get("HTTP_CF_IPCOUNTRY") or "").strip()
    location = ", ".join(part for part in (city, region, country) if part) or "Location not provided by proxy"
    return {
        "ip": ip or "Unknown",
        "location": location,
        "browser": str(meta.get("HTTP_USER_AGENT") or "Unknown")[:1000],
        "forwarded_for": str(meta.get("HTTP_X_FORWARDED_FOR") or "")[:1000],
        "blocked_at": timezone.now().isoformat(),
    }


def _gmail_compose_url(admin_email: str, blocked_user: User) -> str:
    subject = f"Account unblock request - {blocked_user.email}"
    body = (
        f"Hello Administrator,\n\n"
        f"Please review and unblock my OneSmarter MIR Relay account.\n\n"
        f"Account: {blocked_user.email}\n"
        f"Reason shown: Account blocked after 5 unsuccessful password attempts.\n\n"
        f"Thank you."
    )
    return (
        "https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={quote(admin_email)}&su={quote(subject)}&body={quote(body)}"
    )


def send_account_blocked_email(user: User, metadata: dict) -> bool:
    if not user.email or user.client_id is None:
        return False
    admins = list(
        User.objects.filter(is_active=True, is_staff=True)
        .exclude(email="")
        .order_by("-is_superuser", "name", "email")
    )
    admin_rows = []
    for admin in admins:
        role = "Super Admin" if admin.is_superuser else "Admin"
        gmail_url = _gmail_compose_url(admin.email, user)
        admin_rows.append(
            "<tr>"
            f'<td style="padding:9px;border:1px solid #d7e0ea">{escape(admin.name or admin.email)}</td>'
            f'<td style="padding:9px;border:1px solid #d7e0ea">{role}</td>'
            f'<td style="padding:9px;border:1px solid #d7e0ea"><a href="{escape(gmail_url)}">{escape(admin.email)}</a></td>'
            "</tr>"
        )
    admins_html = "".join(admin_rows) or '<tr><td colspan="3" style="padding:9px;border:1px solid #d7e0ea">No active administrator email is currently available.</td></tr>'
    subject = "OneSmarter: Your account has been blocked after 5 failed login attempts"
    html = (
        f'<p>Dear {escape(user.name or user.email)},</p>'
        '<p>Your OneSmarter MIR Relay account has been <strong>blocked</strong> because five unsuccessful password attempts were recorded.</p>'
        '<p>Further login attempts will remain blocked until an administrator restores access.</p>'
        '<h3>Login security details</h3>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">'
        f'<tr><td style="padding:9px;border:1px solid #d7e0ea;font-weight:700">Account</td><td style="padding:9px;border:1px solid #d7e0ea">{escape(user.email)}</td></tr>'
        f'<tr><td style="padding:9px;border:1px solid #d7e0ea;font-weight:700">IP address</td><td style="padding:9px;border:1px solid #d7e0ea">{escape(str(metadata.get("ip") or "Unknown"))}</td></tr>'
        f'<tr><td style="padding:9px;border:1px solid #d7e0ea;font-weight:700">Approx. location</td><td style="padding:9px;border:1px solid #d7e0ea">{escape(str(metadata.get("location") or "Unknown"))}</td></tr>'
        f'<tr><td style="padding:9px;border:1px solid #d7e0ea;font-weight:700">Browser / device</td><td style="padding:9px;border:1px solid #d7e0ea;word-break:break-word">{escape(str(metadata.get("browser") or "Unknown"))}</td></tr>'
        '</table>'
        '<h3>Contact an administrator to request unblocking</h3>'
        '<p>Click an administrator email below to open Gmail with a pre-written unblock request.</p>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">'
        '<tr><th style="padding:9px;border:1px solid #d7e0ea;text-align:left">Name</th><th style="padding:9px;border:1px solid #d7e0ea;text-align:left">Role</th><th style="padding:9px;border:1px solid #d7e0ea;text-align:left">Email</th></tr>'
        + admins_html + '</table>'
        '<p style="margin-top:18px"><strong>If this was not you, contact an administrator immediately.</strong></p>'
    )
    try:
        from admin_panel.email_service import send_client_email
        return bool(send_client_email(user.client, subject, html, to_emails=[user.email]))
    except Exception:
        logger.exception("Failed to send login lockout email for %s", user.email)
        return False


def register_password_failure(user: User, request=None) -> tuple[bool, int]:
    if user.is_staff or user.is_superuser:
        return False, int(user.failed_login_attempts or 0)
    metadata = request_security_metadata(request)
    should_email = False
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        if locked.login_blocked_at:
            return True, int(locked.failed_login_attempts or MAX_FAILED_PASSWORD_ATTEMPTS)
        locked.failed_login_attempts = int(locked.failed_login_attempts or 0) + 1
        fields = ["failed_login_attempts", "updated_at"]
        if locked.failed_login_attempts >= MAX_FAILED_PASSWORD_ATTEMPTS:
            locked.login_blocked_at = timezone.now()
            locked.login_blocked_reason = "Blocked after 5 unsuccessful password attempts."
            locked.login_blocked_metadata = metadata
            fields.extend(["login_blocked_at", "login_blocked_reason", "login_blocked_metadata"])
            should_email = True
        locked.save(update_fields=fields)
        blocked = bool(locked.login_blocked_at)
        attempts = locked.failed_login_attempts
    if should_email:
        send_account_blocked_email(user, metadata)
    return blocked, attempts


def reset_password_failures(user: User) -> None:
    if user.failed_login_attempts or user.login_blocked_at:
        User.objects.filter(pk=user.pk).update(
            failed_login_attempts=0,
            login_blocked_at=None,
            login_blocked_reason="",
            login_blocked_metadata={},
        )
        user.failed_login_attempts = 0
        user.login_blocked_at = None
        user.login_blocked_reason = ""
        user.login_blocked_metadata = {}
