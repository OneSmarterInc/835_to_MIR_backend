"""Administration without implicit access to filenames, claims or file contents."""
import json
from datetime import timedelta

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.paginator import Paginator, EmptyPage
from django.db import transaction, IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from accounts.admin_screens import user_can_access_screen
from accounts.models import Client, User
from .models import (
    IntakePartner, InboundTransfer, IntakeQuarantine, IntakeLegalHold,
    IntakeRetentionPolicy, IntakeOperatingTargets, IntakeStaffGrant, IntakeRecoveryReview,
    IntakeArtifactDisposition, BatchDispatch,
)
from .intake_control import dashboard, control_audit, eligible_artifacts, partner_storage_totals
from .intake_access import permitted_staff
from .partner_management import create_partner, change_partner, validate_reason


def response(data, status=200):
    result = JsonResponse(data, status=status)
    result["Cache-Control"] = "private, no-store"
    return result


def page(query, request):
    number = int(request.GET.get("page", "1"))
    result = Paginator(query, 25).page(number)
    return result, {"page": result.number, "pages": result.paginator.num_pages, "total": result.paginator.count}


def partner_value(partner, stored_bytes):
    return {"id": str(partner.pk), "name": partner.name, "primary_client_id": str(partner.client_id),
        "client_ids": [*([str(partner.client_id)] if partner.primary_client_enabled else []), *[str(c.pk) for c in partner.additional_clients.all()]],
        "active": partner.active, "workflows": partner.workflows, "auth_mode": partner.auth_mode,
        "routing_required": partner.routing_required,
        "requests_per_minute": partner.requests_per_minute, "concurrent_jobs": partner.concurrent_jobs,
        "storage_limit_bytes": partner.storage_limit_bytes, "stored_bytes": stored_bytes,
        "permissions": [{key: getattr(p, key) for key in ("action", "workflow", "channel")} for p in partner.permissions.all()],
        "credentials": [{key: getattr(c, key) for key in ("id", "kind", "reference", "not_before", "expires_at", "active", "revoked_at", "replaces_id")} for c in partner.credentials.all()],
        "routes": [{f.attname: getattr(r, f.attname) for f in r._meta.fields} for r in partner.routes.all()]}


def read(request):
    tab = request.GET.get("tab", "operations")
    if tab == "bootstrap":
        return {"clients": list(Client.objects.exclude(stage="offboarded").order_by("name").values("id", "name", "client_code")),
            "staff": list(User.objects.filter(is_active=True, is_staff=True).order_by("email").values("id", "name", "email")),
            "can_approve": request.user.is_superuser}
    if tab == "operations":
        result = dashboard()
        query = InboundTransfer.objects.filter(state__in=["FAILED", "REJECTED", "QUEUED", "PROCESSING", "VALIDATING"])
        if request.GET.get("client_id"):
            query = query.filter(client_id=request.GET["client_id"])
        if request.GET.get("state"):
            query = query.filter(state=request.GET["state"])
        items, meta = page(query.order_by("-received_at").values(
            "id", "client_id", "workflow", "channel", "state", "safe_error_code", "received_at"), request)
        result.update(meta, rows=list(items))
        return result
    if tab == "partners":
        query = IntakePartner.objects.order_by("name").prefetch_related("additional_clients", "permissions", "credentials", "routes")
        items, meta = page(query, request)
        records = list(items)
        storage = partner_storage_totals([p.pk for p in records])
        return {**meta, "rows": [partner_value(p, storage.get(p.pk, 0)) for p in records]}
    if tab == "quarantine":
        query = IntakeQuarantine.objects.select_related("receipt").order_by("-created_at")
        if request.GET.get("state"):
            query = query.filter(state=request.GET["state"])
        items, meta = page(query.values("id", "receipt_id", "receipt__client_id", "state", "safe_code", "created_at", "reviewed_at"), request)
        return {**meta, "rows": list(items)}
    if tab == "retention":
        client = Client.objects.get(pk=request.GET.get("client_id"))
        holds, meta = page(IntakeLegalHold.objects.filter(client=client).order_by("-created_at").values(
            "id", "receipt_id", "reference", "created_at", "released_at"), request)
        return {**meta, "policy": IntakeRetentionPolicy.objects.filter(client=client).values().first(),
            "rows": list(holds), "eligible_sample_count": len(eligible_artifacts(client)),
            "scope": "Private intake sources and confirmed outputs. Audit and backup periods require external enforcement; legacy archives are not deleted."}
    if tab == "access":
        items, meta = page(IntakeStaffGrant.objects.select_related("user").order_by("-created_at").values(
            "id", "user_id", "user__email", "client_id", "can_download", "can_retry", "expires_at", "revoked_at"), request)
        return {**meta, "rows": list(items), "targets": IntakeOperatingTargets.objects.filter(pk=1).values().first()}
    raise ValueError("Unknown intake control tab.")


def positive(data, name, maximum, nullable=False, minimum=1):
    value = data.get(name)
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a positive integer within its permitted range.")
    return value


def reference(data):
    value = data.get("evidence_reference", "")
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
        raise ValueError("Supply a 3–200 character ticket or evidence reference; do not enter patient data.")
    return value.strip()


@transaction.atomic
def mutate(request, data):
    action, reason = data.get("action"), data.get("reason")
    validate_reason(reason)
    user = request.user
    actor = f"user:{user.pk}"
    privileged = {"grant", "revoke_grant", "targets", "retention", "hold", "release_hold"}
    if action in privileged and not user.is_superuser:
        raise PermissionError("Super Admin approval is required for access, targets, retention and legal holds.")
    if action == "create_partner":
        client = Client.objects.get(pk=data.get("client_id"))
        partner = create_partner(client=client, name=data.get("name"), workflows=data.get("workflows"),
            actor=actor, reason=reason, mode=data.get("auth_mode", "MTLS"), routing_required=True)
        return {"partner_id": str(partner.pk)}
    if action == "partner":
        operation = data.get("operation")
        if operation not in {"SUSPEND", "ACTIVATE", "POLICY", "CLIENTS", "QUOTAS", "PERMISSIONS", "TOKEN", "CERTIFICATE", "RETIRE", "REVOKE", "ROUTE", "RETIRE_ROUTE"}:
            raise ValueError("Unknown partner operation.")
        payload = data.get("data", {})
        if not isinstance(payload, dict):
            raise ValueError("Invalid partner parameters.")
        if operation == "CERTIFICATE":
            pem = payload.get("certificate")
            if not isinstance(pem, str) or len(pem) > 16384:
                raise ValueError("Supply one public PEM certificate, never a private key.")
            payload["certificate"] = pem.encode("ascii")
        return change_partner(data.get("partner_id"), action=operation, actor=actor, reason=reason, data=payload)
    if action == "grant":
        client = Client.objects.select_for_update().get(pk=data.get("client_id"))
        target = User.objects.get(pk=data.get("user_id"), is_staff=True, is_active=True)
        days = positive(data, "days", 365)
        for key in ("can_download", "can_retry"):
            if not isinstance(data.get(key), bool):
                raise ValueError("Choose explicit download and retry permissions.")
        grant = IntakeStaffGrant.objects.create(user=target, client=client, approved_by=user, reason=reason,
            expires_at=timezone.now()+timedelta(days=days), can_download=data["can_download"], can_retry=data["can_retry"])
        control_audit(user, "GRANT_CREATED", reason, client, grant_id=grant.pk, user_id=str(target.pk),
                      can_download=grant.can_download, can_retry=grant.can_retry)
        return {"grant_id": grant.pk}
    if action == "revoke_grant":
        grant = IntakeStaffGrant.objects.select_for_update().get(pk=data.get("id"))
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])
        control_audit(user, "GRANT_REVOKED", reason, grant.client, grant_id=grant.pk)
        return {}
    if action == "targets":
        values = {key: positive(data, key, 31536000, nullable=True, minimum=0 if key == "rpo_seconds" else 1) for key in ("queue_age_seconds", "rpo_seconds", "rto_seconds")}
        values.update(evidence_reference=reference(data), approved_by=user)
        IntakeOperatingTargets.objects.update_or_create(pk=1, defaults=values)
        control_audit(user, "TARGETS_APPROVED", reason, evidence_reference=values["evidence_reference"],
                      targets={key: values[key] for key in ("queue_age_seconds", "rpo_seconds", "rto_seconds")})
        return {}
    if action == "retention":
        client = Client.objects.select_for_update().get(pk=data.get("client_id"))
        values = {key: positive(data, key, 36500, nullable=True) for key in
                  ("source_days", "output_days", "quarantine_days", "audit_days", "backup_days")}
        values.update(evidence_reference=reference(data), approved_by=user)
        IntakeRetentionPolicy.objects.update_or_create(client=client, defaults=values)
        control_audit(user, "RETENTION_APPROVED", reason, client, evidence_reference=values["evidence_reference"],
                      periods={key: value for key, value in values.items() if key.endswith("_days")})
        return {}
    if action == "hold":
        client = Client.objects.select_for_update().get(pk=data.get("client_id"))
        receipt = None
        if data.get("receipt_id"):
            receipt = InboundTransfer.objects.get(pk=data["receipt_id"], client=client)
            if IntakeArtifactDisposition.objects.filter(storage_key=receipt.storage_key, state="PURGED").exists():
                raise ValueError("Source already purged; a hold cannot restore it. Use recovery procedures.")
        hold = IntakeLegalHold.objects.create(client=client, receipt=receipt, created_by=user, reference=reference(data))
        control_audit(user, "LEGAL_HOLD_CREATED", reason, client, hold_id=hold.pk,
                      receipt_id=str(receipt.pk) if receipt else None)
        return {"hold_id": hold.pk}
    if action == "release_hold":
        current = IntakeLegalHold.objects.get(pk=data.get("id"))
        Client.objects.select_for_update().get(pk=current.client_id)
        current = IntakeLegalHold.objects.select_for_update().get(pk=current.pk)
        current.released_at = timezone.now()
        current.save(update_fields=["released_at"])
        control_audit(user, "LEGAL_HOLD_RELEASED", reason, current.client, hold_id=current.pk)
        return {}
    if action in {"quarantine_review", "quarantine_revalidate", "recovery_review"}:
        receipt = InboundTransfer.objects.select_related("client", "dispatch").get(pk=data.get("receipt_id"))
        Client.objects.select_for_update().get(pk=receipt.client_id)
        dispatch = BatchDispatch.objects.select_for_update().get(pk=receipt.dispatch_id)
        receipt.refresh_from_db()
        receipt.dispatch = dispatch
        if action == "recovery_review":
            if data.get("decision") not in {"INVESTIGATING", "DELIVERY_CONFIRMED", "NO_DELIVERY_CONFIRMED"}:
                raise ValueError("Select a recovery finding.")
            review = IntakeRecoveryReview.objects.create(receipt=receipt, actor=user,
                decision=data["decision"], evidence_reference=reference(data))
            control_audit(user, "RECOVERY_REVIEWED", reason, receipt.client, review_id=review.pk,
                          receipt_id=str(receipt.pk), decision=review.decision, evidence_reference=review.evidence_reference)
            return {"message": "Review recorded. No output was resent or completion status overridden."}
        case = IntakeQuarantine.objects.select_for_update().get(receipt=receipt)
        if action == "quarantine_review":
            case.state, case.reviewed_at = "REVIEWED", timezone.now()
            case.save(update_fields=["state", "reviewed_at"])
        else:
            if not permitted_staff(user, receipt.client_id, "RETRY"):
                raise PermissionError("An explicit client retry grant is required.")
            if receipt.state != "REJECTED" or dispatch.state not in {"FAILED", "COMPLETED"}:
                raise ValueError("Only a stopped rejected transfer can be revalidated.")
            # Reuse all conservative retry checks; rejected files never bypass validation.
            from .transfer_service import retry_transfer
            receipt.state, dispatch.state = "FAILED", "FAILED"
            receipt.save(update_fields=["state"])
            dispatch.save(update_fields=["state"])
            retry_transfer(receipt, user, reason)
            case.state = "REVALIDATING"
            case.save(update_fields=["state"])
        control_audit(user, action.upper(), reason, receipt.client, receipt_id=str(receipt.pk))
        return {}
    raise ValueError("Unknown intake control action.")


def intake_control(request):
    operations_read = (request.method == "GET" and request.GET.get("tab", "operations") == "operations"
                       and user_can_access_screen(request.user, "ops"))
    if not operations_read and not user_can_access_screen(request.user, "intake-control"):
        return response({"error": "Intake Control screen permission is required."}, 403)
    try:
        if request.method == "GET":
            result = read(request)
            from .intake_access import audit_access
            audit_access(request, "CONTROL_METADATA_READ")
            return response(result)
        if request.method != "POST":
            return response({"error": "Use GET or POST."}, 405)
        if request.content_type != "application/json" or len(request.body) > 65536:
            return response({"error": "Use a JSON body up to 64 KiB."}, 400)
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError("Supply a JSON object.")
        return response({"success": True, **mutate(request, data)})
    except PermissionError as exc:
        return response({"error": str(exc)}, 403)
    except (ObjectDoesNotExist, ValidationError, EmptyPage, IntegrityError, UnicodeError):
        return response({"error": "Record unavailable, conflicting, or invalid. Refresh and check your selection."}, 400)
    except (ValueError, TypeError) as exc:
        return response({"error": str(exc) if isinstance(exc, ValueError) else "Invalid input."}, 400)
