"""Shared persistence and read API for operational claim alert emails."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from admin_panel.email_service import get_client_users

from .alert_models import ClaimAlertEmail


PENDING_RETRY_AFTER = timedelta(minutes=15)


def alert_recipients(client) -> list[str]:
    recipients = {str(value).strip() for value in get_client_users(client) if str(value).strip()}
    if getattr(client, "email", ""):
        recipients.add(str(client.email).strip())
    return sorted(value for value in recipients if value)


def reserve_daily_alert(*, client, category, alert_date, subject, recipients, claims, body_text=""):
    """Reserve one consolidated email per client/category/Eastern day."""
    now = timezone.now()
    with transaction.atomic():
        row = (
            ClaimAlertEmail.objects.select_for_update()
            .filter(client=client, category=category, alert_date=alert_date)
            .first()
        )
        if row is not None:
            if row.status == "SENT":
                return None
            if row.updated_at and row.updated_at > now - PENDING_RETRY_AFTER:
                return None
            row.status = "PENDING"
            row.subject = subject
            row.recipients = list(recipients or [])
            row.claims = list(claims or [])
            row.body_text = str(body_text or "")
            row.error_message = ""
            row.sent_at = None
            row.save(update_fields=[
                "status", "subject", "recipients", "claims", "body_text",
                "error_message", "sent_at", "updated_at",
            ])
            return row

        try:
            return ClaimAlertEmail.objects.create(
                client=client,
                category=category,
                alert_date=alert_date,
                status="PENDING",
                subject=subject,
                recipients=list(recipients or []),
                claims=list(claims or []),
                body_text=str(body_text or ""),
            )
        except IntegrityError:
            return None


def mark_alert_sent(row, *, sent_at=None):
    sent_at = sent_at or timezone.now()
    ClaimAlertEmail.objects.filter(id=row.id).update(
        status="SENT", sent_at=sent_at, error_message="", updated_at=sent_at,
    )


def mark_alert_failed(row, error):
    ClaimAlertEmail.objects.filter(id=row.id).update(
        status="FAILED",
        error_message=str(error or "Email delivery failed.")[:2000],
        updated_at=timezone.now(),
    )


def record_sent_alert(*, client, category, alert_date, subject, recipients, claims, body_text="", sent_at=None):
    """Record an already-successful alert without creating duplicate daily rows."""
    sent_at = sent_at or timezone.now()
    row, _created = ClaimAlertEmail.objects.update_or_create(
        client=client,
        category=category,
        alert_date=alert_date,
        defaults={
            "status": "SENT",
            "subject": subject,
            "recipients": list(recipients or []),
            "claims": list(claims or []),
            "body_text": str(body_text or ""),
            "sent_at": sent_at,
            "error_message": "",
        },
    )
    return row


def _visible_alerts(request):
    qs = ClaimAlertEmail.objects.filter(status="SENT").select_related("client")
    user = request.user
    if user.is_staff:
        if not user.is_superuser:
            from admin_panel.access_control import active_client_grant_ids
            qs = qs.filter(client_id__in=active_client_grant_ids(user))
    else:
        qs = qs.filter(client=getattr(user, "client", None))

    requested_client_id = str(request.GET.get("client_id") or "").strip()
    if requested_client_id:
        qs = qs.filter(client_id=requested_client_id)

    return qs.order_by("-sent_at", "-created_at")


def api_claim_alert_email_history(request):
    """Return sent claim alert emails scoped to the selected/visible client."""
    payload = []
    for row in _visible_alerts(request):
        payload.append({
            "id": str(row.id),
            "client_id": str(row.client_id),
            "client_name": row.client.name,
            "category": row.category,
            "category_label": row.get_category_display(),
            "alert_date": row.alert_date.isoformat(),
            "subject": row.subject,
            "recipients": list(row.recipients or []),
            "claims": list(row.claims or []),
            "body_text": row.body_text or "",
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        })
    return JsonResponse({"success": True, "emails": payload})
