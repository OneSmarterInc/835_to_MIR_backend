"""Audited partner administration, shared by operator tooling.

All changes serialize on client -> partner, matching the intake lock order.
Routes are replaced, never edited, so receipt snapshots retain their meaning.
"""
import hashlib
import json
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import Client
from admin_panel.models import AuditLog
from .models import IntakePartner, PartnerCredential, PartnerPermission, PartnerRoute
from .partner_auth import certificate_identity
from .partner_routing import ROUTE_FIELDS


def audit(partner, action, actor, reason, **metadata):
    AuditLog.objects.create(module="Partner intake", action=action, client=partner.client,
        performed_by=actor, details=json.dumps({"partner_id": str(partner.pk), "reason": reason, **metadata}, sort_keys=True))


def validate_reason(reason):
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500 or any(ord(c) < 32 for c in reason):
        raise ValueError("Supply an audit reason of 3–500 characters without control characters.")


@transaction.atomic
def create_partner(*, client, name, workflows, actor, reason, mode="TOKEN", routing_required=True):
    validate_reason(reason)
    client = Client.objects.select_for_update().get(pk=client.pk)
    if client.status != "ACTIVE" or client.stage == "offboarded":
        raise ValueError("An active client is required.")
    if mode not in {"TOKEN", "MTLS"} or not workflows or set(workflows) - {"835", "837"}:
        raise ValueError("Select TOKEN/MTLS and workflows 835/837.")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("Partner name must contain 1–100 characters.")
    partner = IntakePartner.objects.create(client=client, name=name.strip(), workflows=sorted(set(workflows)),
        token_digest="", expires_at=timezone.now(), auth_mode=mode, routing_required=routing_required)
    audit(partner, "CREATED", actor, reason, workflows=partner.workflows, auth_mode=mode)
    # New identities have no permission until explicitly approved.
    return partner


@transaction.atomic
def change_partner(partner_id, *, action, actor, reason, data=None):
    validate_reason(reason)
    data = data or {}
    current = IntakePartner.objects.get(pk=partner_id)
    Client.objects.select_for_update().get(pk=current.client_id)
    partner = IntakePartner.objects.select_for_update().get(pk=partner_id)
    result, metadata = {}, {}
    if action in {"SUSPEND", "ACTIVATE"}:
        partner.active = action == "ACTIVATE"
        partner.save(update_fields=["active"])
    elif action == "POLICY":
        mode = data.get("auth_mode", partner.auth_mode)
        if mode not in {"TOKEN", "MTLS"} or not isinstance(data.get("routing_required", True), bool):
            raise ValueError("Invalid authentication policy.")
        partner.auth_mode, partner.routing_required = mode, data.get("routing_required", True)
        if mode == "MTLS" and not partner.credentials.filter(kind="CERTIFICATE", active=True,
                revoked_at__isnull=True, not_before__lte=timezone.now(), expires_at__gt=timezone.now()).exists():
            raise ValueError("Register a currently valid client certificate before requiring mTLS.")
        partner.save(update_fields=["auth_mode", "routing_required"])
        metadata = {"auth_mode": mode, "routing_required": partner.routing_required}
    elif action == "PERMISSIONS":
        permissions = data.get("permissions")
        if not isinstance(permissions, list) or len(permissions) > 6:
            raise ValueError("Supply up to six explicit permission records.")
        approved = set()
        for permission in permissions:
            if not isinstance(permission, dict):
                raise ValueError("Invalid permission.")
            action_name, workflow = permission.get("action"), permission.get("workflow")
            if (action_name not in {"SUBMIT", "READ", "DOWNLOAD"} or workflow not in partner.workflows
                    or permission.get("channel", "PARTNER") != "PARTNER"):
                raise ValueError("Permission must use an approved workflow, PARTNER channel and SUBMIT/READ/DOWNLOAD action.")
            approved.add((action_name, workflow))
        partner.permissions.all().delete()
        PartnerPermission.objects.bulk_create([PartnerPermission(partner=partner, action=a, workflow=w) for a, w in sorted(approved)])
        metadata = {"permissions": sorted(approved)}
    elif action in {"TOKEN", "CERTIFICATE"}:
        replacement = None
        if data.get("replaces"):
            replacement = partner.credentials.get(pk=data["replaces"])
        if action == "TOKEN":
            days = data.get("days", 30)
            if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
                raise ValueError("Token expiry must be 1–90 days.")
            secret = secrets.token_urlsafe(32)
            digest = hashlib.sha256(secret.encode()).hexdigest()
            start, end = timezone.now(), timezone.now() + timedelta(days=days)
        else:
            digest, start, end = certificate_identity(data.get("certificate", b""))
            if end <= timezone.now():
                raise ValueError("An expired certificate cannot be registered.")
        credential = PartnerCredential.objects.create(partner=partner, kind=action, digest=digest,
            reference=str(data.get("reference", ""))[:150], not_before=start, expires_at=end, replaces=replacement)
        metadata = {"credential_id": str(credential.pk), "kind": action, "expires_at": end.isoformat(),
                    "replaces": str(replacement.pk) if replacement else None}
        result = dict(metadata)
        if action == "TOKEN":
            result["token"] = f"{credential.pk}.{secret}"
        # Old credentials intentionally stay usable during the overlap window.
    elif action in {"RETIRE", "REVOKE"}:
        credential = partner.credentials.select_for_update().get(pk=data.get("credential_id"))
        credential.active = False
        if action == "REVOKE":
            credential.revoked_at, credential.revocation_reason = timezone.now(), reason
        credential.save(update_fields=["active", "revoked_at", "revocation_reason"])
        metadata = {"credential_id": str(credential.pk)}
    elif action == "ROUTE":
        if data.get("workflow") not in partner.workflows or set(data) - set(ROUTE_FIELDS) - {"workflow"}:
            raise ValueError("Route must use an approved workflow and exact envelope fields.")
        for field in ROUTE_FIELDS:
            if not isinstance(data.get(field), str) or (not data[field] and field != "st_version"):
                raise ValueError("Supply every envelope field; only st_version may be empty.")
            if data[field] != data[field].strip(" ") or any(ord(c) < 33 or ord(c) > 126 for c in data[field]):
                raise ValueError("Route identifiers must be exact printable ASCII without padding.")
        if len(data["isa_sender_qualifier"]) != 2 or len(data["isa_receiver_qualifier"]) != 2 or len(data["isa_version"]) != 5:
            raise ValueError("ISA qualifiers must have two characters and ISA version five.")
        route = PartnerRoute(partner=partner, client=partner.client, approved_by=actor, **data)
        try:
            route.full_clean()
        except ValidationError as exc:
            raise ValueError("Invalid or duplicate route. Retire the old route before replacing it.") from exc
        route.save()
        metadata = result = {"route_id": route.pk}
    elif action == "RETIRE_ROUTE":
        route = partner.routes.get(pk=data.get("route_id"))
        route.active = False
        route.save(update_fields=["active"])
        metadata = {"route_id": route.pk}
    else:
        raise ValueError("Unsupported partner operation.")
    audit(partner, action, actor, reason, **metadata)
    return result
