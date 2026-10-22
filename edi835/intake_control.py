"""Intake operations: safe diagnostics, quarantine and hold-aware retention."""
import json
import shutil
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Min, Sum, Q, OuterRef, Subquery, Value
from django.utils import timezone
from admin_panel.models import AuditLog
from accounts.models import Client
from .models import (
    InboundTransfer, IntakeOutput, IntakePartner, PartnerCredential, ProcessingAttempt,
    IntakeQuarantine, IntakeLegalHold, IntakeRetentionPolicy, IntakeArtifactDisposition,
    IntakeOperatingTargets, BatchDispatch,
)
from .intake_storage import storage_root, source_path, _sync_directory


class QuotaExceeded(ValueError):
    pass


def control_audit(user, action, reason, client=None, **safe):
    from .partner_management import validate_reason
    validate_reason(reason)
    return AuditLog.objects.create(module="Intake control", action=action, client=client,
        performed_by=f"user:{user.pk}" if user else "system:intake-retention",
        details=json.dumps({"reason": reason, **safe}, sort_keys=True))


def partner_storage_bytes(partner):
    disposed = IntakeArtifactDisposition.objects.filter(state="PURGED").values("storage_key")
    receipts = InboundTransfer.objects.filter(partner=partner)
    inputs = receipts.exclude(storage_key__in=disposed).aggregate(n=Sum("byte_count"))["n"] or 0
    outputs = IntakeOutput.objects.filter(dispatch_id__in=receipts.values("dispatch_id")).exclude(storage_key__in=disposed)
    return inputs + (outputs.aggregate(n=Sum("byte_count"))["n"] or 0)


def partner_storage_totals(partner_ids):
    disposed = IntakeArtifactDisposition.objects.filter(state="PURGED").values("storage_key")
    inputs = (InboundTransfer.objects.filter(partner_id=OuterRef("pk")).exclude(storage_key__in=disposed)
              .order_by().values("partner_id").annotate(n=Sum("byte_count")).values("n"))
    dispatches = InboundTransfer.objects.filter(partner_id=OuterRef(OuterRef("pk"))).values("dispatch_id")
    outputs = (IntakeOutput.objects.filter(dispatch_id__in=Subquery(dispatches)).exclude(storage_key__in=disposed)
               .order_by().annotate(group=Value(1)).values("group").annotate(n=Sum("byte_count")).values("n"))
    # Match quota enforcement, including shared canonical outputs, without one
    # aggregate request per displayed partner or duplicate output joins.
    rows = IntakePartner.objects.filter(pk__in=partner_ids).annotate(
        input_bytes=Subquery(inputs), output_bytes=Subquery(outputs)).values("pk", "input_bytes", "output_bytes")
    return {row["pk"]: (row["input_bytes"] or 0) + (row["output_bytes"] or 0) for row in rows}


def ensure_quarantine(receipts):
    for receipt in receipts.filter(Q(state="REJECTED") | Q(validation_outcome="REJECTED")):
        IntakeQuarantine.objects.get_or_create(receipt=receipt,
            defaults={"safe_code": receipt.safe_error_code or "VALIDATION_REJECTED"})
        IntakeQuarantine.objects.filter(receipt=receipt, state="REVALIDATING").update(state="OPEN", reviewed_at=None)


def dashboard():
    now = timezone.now()
    rows = InboundTransfer.objects.all()
    counts = dict(rows.values_list("state").annotate(n=Count("id")))
    oldest = rows.filter(state="QUEUED").aggregate(at=Min("received_at"))["at"]
    targets = IntakeOperatingTargets.objects.filter(pk=1).values().first()
    try:
        usage = shutil.disk_usage(storage_root())
        disk = {"total_bytes": usage.total, "free_bytes": usage.free, "used_bytes": usage.used}
    except OSError:
        disk = None
    return {"as_of": now, "counts": counts,
        "oldest_queue_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "expired_leases": ProcessingAttempt.objects.filter(state="RUNNING", lease_expires_at__lte=now).count(),
        "last_worker_heartbeat": ProcessingAttempt.objects.filter(dispatch__payload__job_type="INTAKE")
            .order_by("-heartbeat_at").values_list("heartbeat_at", flat=True).first(),
        "quarantine_open": IntakeQuarantine.objects.filter(state="OPEN").count(),
        "delivery_unconfirmed": IntakeOutput.objects.filter(delivery_state__in=["SENDING", "UNCONFIRMED"]).count(),
        "expiring_credentials": PartnerCredential.objects.filter(active=True, revoked_at__isnull=True,
            expires_at__lte=now + timedelta(days=14)).count(),
        "active_holds": IntakeLegalHold.objects.filter(released_at__isnull=True).count(),
        "disk": disk, "targets": targets}


def hold_applies(client_id, receipt_ids):
    return IntakeLegalHold.objects.filter(client_id=client_id, released_at__isnull=True).filter(
        Q(receipt__isnull=True) | Q(receipt_id__in=receipt_ids)).exists()


def eligible_artifacts(client, limit=100):
    """Private intake files only; legacy archives, audit and backups are separate."""
    policy = IntakeRetentionPolicy.objects.filter(client=client).first()
    if not policy:
        return []
    active_holds = IntakeLegalHold.objects.filter(client=client, released_at__isnull=True)
    if active_holds.filter(receipt__isnull=True).exists():
        return []
    held_receipts = active_holds.values("receipt_id")
    now = timezone.now()
    disposed = IntakeArtifactDisposition.objects.values("storage_key")
    # Use SQL date filters rather than reading all historical claim/file content.
    terminal = InboundTransfer.objects.filter(client=client, state__in=["COMPLETED", "DUPLICATE", "REJECTED"],
        dispatch__state__in=["COMPLETED", "FAILED"]).exclude(storage_key__in=disposed).exclude(pk__in=held_receipts)
    selected = []
    for rejected, days in ((False, policy.source_days), (True, policy.quarantine_days)):
        if days is None:
            continue
        query = terminal.filter(received_at__lt=now - timedelta(days=days))
        query = query.filter(state="REJECTED") if rejected else query.exclude(state="REJECTED")
        if rejected:
            query = query.filter(quarantine__state="REVIEWED")
        for receipt in query.order_by("received_at")[:limit]:
            selected.append((receipt.storage_key, "QUARANTINE" if rejected else "SOURCE", [receipt.pk]))
    if policy.output_days is not None:
        outputs = IntakeOutput.objects.filter(dispatch__transfers__client=client, dispatch__state="COMPLETED",
            delivery_state="CONFIRMED", created_at__lt=now - timedelta(days=policy.output_days)).exclude(
            storage_key__in=disposed).exclude(dispatch_id__in=InboundTransfer.objects.filter(
                pk__in=held_receipts).values("dispatch_id")).exclude(dispatch_id__in=InboundTransfer.objects.exclude(
                    client=client).values("dispatch_id")).distinct().prefetch_related("dispatch__transfers")
        for output in outputs[:limit]:
            selected.append((output.storage_key, "OUTPUT", [r.pk for r in output.dispatch.transfers.all()]))
    return selected[:limit]


def apply_retention(client_id, *, limit=100):
    """Persist tombstones first; unlink under the same client lock used by holds.

    A failed unlink remains PENDING and retryable; metadata/audit/idempotency rows
    stay intact. Nothing outside private intake storage is removed.
    """
    with transaction.atomic():
        client = Client.objects.select_for_update().get(pk=client_id)
        for key, kind, _ in eligible_artifacts(client, limit):
            IntakeArtifactDisposition.objects.get_or_create(storage_key=key, defaults={"client": client, "kind": kind})
    count = 0
    for identifier in list(IntakeArtifactDisposition.objects.filter(client_id=client_id, state="PENDING").values_list("pk", flat=True)[:limit]):
        with transaction.atomic():
            Client.objects.select_for_update().get(pk=client_id)
            item = IntakeArtifactDisposition.objects.select_for_update().get(pk=identifier)
            if item.state != "PENDING":
                continue
            receipts = InboundTransfer.objects.filter(storage_key=item.storage_key)
            if item.kind == "OUTPUT":
                receipts = InboundTransfer.objects.filter(dispatch__outputs__storage_key=item.storage_key)
            if hold_applies(client_id, list(receipts.values_list("pk", flat=True))):
                continue
            if (not receipts.exists() or receipts.exclude(client_id=client_id).exists()
                    or receipts.exclude(state__in=["COMPLETED", "DUPLICATE", "REJECTED"]).exists()
                    or receipts.exclude(dispatch__state__in=["COMPLETED", "FAILED"]).exists()):
                continue
            if item.kind == "QUARANTINE" and receipts.exclude(quarantine__state="REVIEWED").exists():
                continue
            if item.kind == "OUTPUT" and not IntakeOutput.objects.filter(
                    storage_key=item.storage_key, delivery_state="CONFIRMED", dispatch__state="COMPLETED").exists():
                continue
            policy = IntakeRetentionPolicy.objects.filter(client_id=client_id).first()
            days = getattr(policy, {"SOURCE": "source_days", "QUARANTINE": "quarantine_days", "OUTPUT": "output_days"}[item.kind], None)
            created = (IntakeOutput.objects.filter(storage_key=item.storage_key).values_list("created_at", flat=True).first()
                       if item.kind == "OUTPUT" else receipts.values_list("received_at", flat=True).first())
            if days is None or not created or created >= timezone.now() - timedelta(days=days):
                continue
            path = source_path(item.storage_key)
            path.unlink(missing_ok=True)
            if path.parent.exists():
                _sync_directory(path.parent)
            item.state, item.purged_at = "PURGED", timezone.now()
            item.save(update_fields=["state", "purged_at"])
            control_audit(None, "ARTIFACT_PURGED", "Approved retention policy", client,
                          disposition_id=item.pk, kind=item.kind)
            count += 1
    return count
