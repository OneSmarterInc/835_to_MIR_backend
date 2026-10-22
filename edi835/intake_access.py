"""Explicit intake permissions and payload-free access audit."""
import json
import uuid

from django.utils import timezone
from admin_panel.models import AuditLog
from .models import IntakeStaffGrant


def grants(user):
    if not user.is_authenticated or not user.is_active or not user.is_staff:
        return IntakeStaffGrant.objects.none()
    return IntakeStaffGrant.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=timezone.now())


def permitted_staff(user, client_id, action="READ"):
    query = grants(user).filter(client_id=client_id)
    if action == "DOWNLOAD":
        query = query.filter(can_download=True)
    elif action == "RETRY":
        query = query.filter(can_retry=True)
    return query.exists()


def audit_access(request, action, *, receipt=None, client=None, result="ALLOWED", **safe):
    """Never put filenames, claim data, credentials or free-form request data here."""
    request_id = getattr(request, "intake_request_id", None) or str(uuid.uuid4())
    request.intake_request_id = request_id
    credential = getattr(request, "partner_credential", None)
    actor = f"partner:{credential.partner_id}" if credential else f"user:{getattr(request.user, 'pk', None)}"
    AuditLog.objects.create(module="Intake access", action=action, performed_by=actor,
        client=receipt.client if receipt else client,
        details=json.dumps({"request_id": request_id, "receipt_id": str(receipt.pk) if receipt else None,
                            "result": result, **safe}, sort_keys=True))
    return request_id
