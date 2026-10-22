"""Versioned submission recovery and conservative, audited operator retry."""
import hashlib
from contextlib import contextmanager

from django.conf import settings
from django.db import connection, transaction

from .intake_service import register_sources
from .intake_storage import open_verified, source_path, store_stream
from .intake_upload_policy import check_source
from .models import BatchDispatch, SubmissionKey, TransferRetry


class SubmissionConflict(ValueError):
    code = "IDEMPOTENCY_CONFLICT"


class SubmissionPending(SubmissionConflict):
    code = "SUBMISSION_IN_PROGRESS"


def key_digest(key: str, *, user=None, partner=None) -> str:
    if not 1 <= len(key) <= 200 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Idempotency-Key must contain 1–200 printable non-space ASCII characters.")
    principal = f"partner:{partner.pk}" if partner else f"user:{user.pk}"
    return hashlib.sha256(f"transfers:v1:{principal}:{key}".encode()).hexdigest()


@contextmanager
def discard_quota_rejection(source):
    from .intake_control import QuotaExceeded
    from .models import InboundTransfer
    try:
        yield
    except QuotaExceeded:
        # Runs after atomic rollback, only for this newly allocated private key.
        if not InboundTransfer.objects.filter(storage_key=source.key).exists():
            source_path(source.key).unlink(missing_ok=True)
        raise


def submit(*, client, user, partner, workflow: str, upload, digest: str, partner_credential=None):
    source = store_stream(upload, client.pk, max_bytes=settings.INTAKE_MAX_BYTES)
    check_source(upload.name, source)
    with discard_quota_rejection(source), transaction.atomic():
        # A nonblocking transaction lock makes unresolved same-key submissions
        # explicit. The PK remains the durable uniqueness guarantee.
        if connection.vendor == "postgresql":
            lock_id = int.from_bytes(bytes.fromhex(digest)[:8], "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_id])
                if not cursor.fetchone()[0]:
                    source_path(source.key).unlink(missing_ok=True)
                    raise SubmissionPending("This submission is still committing. Retry with the same key.")
        existing = SubmissionKey.objects.select_related("receipt__dispatch").filter(pk=digest).first()
        if existing:
            receipt = existing.receipt
            source_path(source.key).unlink(missing_ok=True)
            if (receipt.client_id != client.pk or receipt.workflow != workflow
                    or receipt.checksum != source.checksum or receipt.byte_count != source.size):
                raise SubmissionConflict("This key belongs to different content or routing.")
            return receipt, True
        receipts, _ = register_sources(client=client, actor=user, partner=partner,
            channel="PARTNER" if partner else "STAFF", workflow=workflow,
            sources=[(upload.name, source)], asynchronous=True, partner_credential=partner_credential)
        receipt = receipts[0]
        SubmissionKey.objects.create(digest=digest, receipt=receipt)
    return receipt, False


def retry_blocker(receipt) -> str:
    dispatch = receipt.dispatch
    if receipt.canonical_transfer_id or receipt.state != "FAILED" or dispatch.state != "FAILED":
        return "Only failed canonical transfers can be retried."
    if dispatch.payload.get("job_type") != "INTAKE" or dispatch.transfers.count() != 1:
        return "This workflow requires its existing recovery procedure."
    if dispatch.attempts.filter(state="RUNNING").exists():
        return "A processing lease must be reconciled before retry."
    if dispatch.outputs.exists() or dispatch.attempts.filter(effects_started_at__isnull=False).exists():
        return "Processing effects require reconciliation; automatic replay is unsafe."
    if dispatch.attempts.count() >= settings.INTAKE_MAX_ATTEMPTS:
        return "The configured attempt limit has been reached."
    if receipt.client.status != "ACTIVE" or receipt.client.stage == "offboarded":
        return "The client is not active."
    if receipt.partner_id:
        from .partner_auth import accepted_authorization_valid
        if not accepted_authorization_valid(receipt):
            return "The original partner authorization is no longer valid."
    return ""


@transaction.atomic
def retry_transfer(receipt, user, reason: str):
    from accounts.models import Client
    from .intake_access import permitted_staff
    # Use the same client -> dispatch lock order as registration.
    Client.objects.select_for_update().get(pk=receipt.client_id)
    if not permitted_staff(user, receipt.client_id, "RETRY"):
        raise ValueError("Current operator authorization is required.")
    dispatch = BatchDispatch.objects.select_for_update().get(pk=receipt.dispatch_id)
    receipt.refresh_from_db()
    receipt.dispatch = dispatch
    blocker = retry_blocker(receipt)
    if blocker:
        raise ValueError(blocker)
    from .models import InboundTransfer
    if InboundTransfer.objects.filter(client_id=receipt.client_id,
            dispatch__state__in=["QUEUED", "RUNNING"]).count() >= settings.INTAKE_MAX_PENDING_PER_CLIENT:
        raise ValueError("Client intake queue is full.")
    with open_verified(receipt):
        pass
    audit = TransferRetry.objects.create(receipt=receipt, actor=user,
        actor_reference=f"user:{user.pk}", reason=reason,
        parent=dispatch.attempts.order_by("-sequence").first())
    job = dict(dispatch.payload)
    for field in ("attempt_id", "lease_owner", "not_before", "intake_result", "receipt_state"):
        job.pop(field, None)
    job.update(state="QUEUED", retry_reason="OPERATOR_RETRY", retry_request_id=str(audit.pk),
        owner_user_id=str(user.pk), result=None, safe_error_code="", finished_at=None,
        worker_started_at=None, status_code=None)
    dispatch.state, dispatch.payload = "QUEUED", job
    dispatch.save(update_fields=["state", "payload", "updated_at"])
    receipt.state, receipt.safe_error_code, receipt.validation_outcome = "QUEUED", "", "PENDING"
    receipt.save(update_fields=["state", "safe_error_code", "validation_outcome", "updated_at"])
    return audit
