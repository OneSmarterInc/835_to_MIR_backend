"""Shared persistence and read API for operational claim alert emails."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from admin_panel.email_service import get_client_users

from .alert_models import ClaimAlertEmail


PENDING_RETRY_AFTER = timedelta(minutes=15)
EASTERN = ZoneInfo("America/New_York")
SCHEDULED_ALERT_TIME = time(hour=17, minute=30)


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


def _visible_alerts(request, *, include_unsent=False):
    qs = ClaimAlertEmail.objects.select_related("client")
    if not include_unsent:
        qs = qs.filter(status="SENT")
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


def _visible_client(request):
    """Return the one client selected/owned by this request without widening access."""
    from accounts.models import Client

    qs = Client.objects.all()
    user = request.user
    if user.is_staff:
        if not user.is_superuser:
            from admin_panel.access_control import active_client_grant_ids
            qs = qs.filter(id__in=active_client_grant_ids(user))
    else:
        qs = qs.filter(id=getattr(user, "client_id", None))

    requested_client_id = str(request.GET.get("client_id") or "").strip()
    if requested_client_id:
        qs = qs.filter(id=requested_client_id)
    return qs.first()


def _serialize_row(row):
    return {
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
        "status": "SENT" if row.status == "SENT" else "NOT_SENT",
        "internal_status": row.status,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "error_message": row.error_message or "",
    }


def _preview_missing_reference(client, preview_now):
    from .missing_reference_alerts import _collect_due_missing

    items = list(_collect_due_missing(preview_now).get(str(client.id), []) or [])
    if not items:
        return None
    return {
        "category": "MISSING_REFERENCE",
        "category_label": "Missing 837 / RECON",
        "subject": f"OneSmarter: Daily Alert - {len(items)} Claim(s) Missing 837 / RECON After 7 Days",
        "recipients": alert_recipients(client),
        "claims": [
            {
                "claim_number": item["claim_number"],
                "claim_control_number": str(item["claim_control_number"] or "").strip(),
                "missing_in": list(item["missing_in"]),
                "missing_in_label": item["missing_in_label"],
                "sent_at": item["sent_at"].isoformat(),
                "eligible_at": item["eligible_at"].isoformat(),
                "mir_filename": item["mir_filename"],
            }
            for item in items
        ],
    }


def _preview_conversion_hold(client, preview_now):
    from .long_hold_alerts import _collect_overdue

    items = list(_collect_overdue(preview_now).get(str(client.id), []) or [])
    if not items:
        return None
    return {
        "category": "CONVERSION_HOLD",
        "category_label": "Conversion hold",
        "subject": f"OneSmarter: Daily Alert - {len(items)} Unresolved Claim(s) Held More Than 7 Days",
        "recipients": alert_recipients(client),
        "claims": [
            {
                "claim_number": item["claim_number"],
                "source_835_filename": item["source_835_filename"],
                "held_since": item["held_since"].isoformat(),
                "days_held": item["days_held"],
                "alert_number": item["alert_number"],
                "alert_limit": item["alert_limit"],
                "reasons": list(item["reasons"]),
            }
            for item in items
        ],
    }


def _scheduled_alerts_for_date(request, selected_date):
    client = _visible_client(request)
    if client is None:
        return JsonResponse(
            {"success": False, "error": "Select a client before viewing scheduled alert emails."},
            status=400,
        )

    scheduled_at = datetime.combine(selected_date, SCHEDULED_ALERT_TIME, tzinfo=EASTERN)
    # Run previews just after the daily 5:30 PM Eastern checkpoint so the
    # missing-reference collector includes claims eligible on this date.
    preview_now = scheduled_at + timedelta(seconds=1)

    existing_rows = {
        row.category: row
        for row in _visible_alerts(request, include_unsent=True).filter(alert_date=selected_date)
    }

    previews = {
        "MISSING_REFERENCE": _preview_missing_reference(client, preview_now),
        "CONVERSION_HOLD": _preview_conversion_hold(client, preview_now),
    }

    payload = []
    for category in ("MISSING_REFERENCE", "CONVERSION_HOLD"):
        existing = existing_rows.get(category)
        if existing is not None:
            item = _serialize_row(existing)
            item["scheduled_at"] = scheduled_at.isoformat()
            payload.append(item)
            continue

        preview = previews.get(category)
        if not preview:
            continue
        payload.append({
            "id": f"scheduled-{category}-{selected_date.isoformat()}",
            "client_id": str(client.id),
            "client_name": client.name,
            "alert_date": selected_date.isoformat(),
            "scheduled_at": scheduled_at.isoformat(),
            "status": "NOT_SENT",
            "internal_status": "SCHEDULED",
            "sent_at": None,
            "body_text": "",
            "error_message": "",
            **preview,
        })

    return JsonResponse({
        "success": True,
        "selected_date": selected_date.isoformat(),
        "emails": payload,
    })


def api_claim_alert_email_history(request):
    """Return sent history, or a selected-date schedule preview for one client."""
    requested_date = str(request.GET.get("date") or "").strip()
    if requested_date:
        try:
            selected_date = datetime.strptime(requested_date, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"success": False, "error": "Date must use YYYY-MM-DD format."}, status=400)
        return _scheduled_alerts_for_date(request, selected_date)

    payload = [_serialize_row(row) for row in _visible_alerts(request)]
    return JsonResponse({"success": True, "emails": payload})
