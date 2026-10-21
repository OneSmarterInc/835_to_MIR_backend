"""One registration boundary for portal, SFTP, Analysis and partner submissions."""
import hashlib
import io
import uuid
from contextvars import ContextVar

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import Client
from .intake_storage import StoredSource, source_path, store_stream
from .models import BatchDispatch, InboundTransfer


current_transfer = ContextVar("current_intake_transfer", default=None)


def register_sources(*, client, actor, channel, workflow, sources, partner=None,
                     idempotency_key=None, asynchronous=False, max_bytes=None):
    """Finalize bytes, then atomically commit every receipt and its dispatch intent.

    On a DB rollback finalized objects remain private and unreferenced. They are
    not deleted here: caller-owned outer transactions and uncertain commits must
    not turn a committed receipt into missing bytes. Reconciliation reports them.
    """
    if channel not in {"STAFF", "SFTP", "PARTNER", "ANALYSIS", "SYSTEM"}:
        raise ValueError("Unknown intake channel.")
    if workflow not in {"835", "837", "VALIDATE_835", "ANALYSIS_835", "ANALYSIS_837", "RECON"}:
        raise ValueError("Unknown intake workflow.")
    if client and str(client.stage).lower() == "offboarded":
        raise ValueError("The selected client is offboarded.")
    if idempotency_key and (not client or len(idempotency_key) > 200 or len(sources) != 1):
        raise ValueError("Idempotency requires one source and a key of at most 200 characters.")
    if asynchronous and (not client or workflow not in {"835", "837"}):
        raise ValueError("Asynchronous intake requires an assigned client and 835/837 workflow.")
    started = timezone.now()
    actor_id = getattr(actor, "pk", None) if getattr(actor, "is_authenticated", False) else None
    actor_ref = f"partner:{partner.pk}" if partner else (f"user:{actor_id}" if actor_id else "system")
    stored = []
    owned_keys = set()
    for filename, stream in sources:
        name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or name in {".", ".."} or len(name) > 255 or any(ord(c) < 32 for c in name):
            raise ValueError("A valid original filename is required.")
        if isinstance(stream, StoredSource):
            scope = str(client.pk) if client else "system"
            if not stream.key.startswith(scope + "/") or stream.size > (max_bytes or settings.INTAKE_MAX_BYTES):
                raise ValueError("Stored source does not match this intake scope or size limit.")
            source = stream
        else:
            source = store_stream(stream, getattr(client, "pk", None),
                                  max_bytes=max_bytes or settings.INTAKE_MAX_BYTES)
            owned_keys.add(source.key)
        stored.append((name, source))
    if not stored:
        raise ValueError("No source files supplied.")
    key_digest = None
    if idempotency_key:
        key_digest = hashlib.sha256(
            f"{client.pk}:{actor_ref}:{channel}:{workflow}:{idempotency_key}".encode()
        ).hexdigest()
    with transaction.atomic():
        # Serialize per-client key checks, including concurrent first submissions.
        if client:
            locked = Client.objects.select_for_update().get(pk=client.pk)
            if str(locked.stage).lower() == "offboarded":
                raise ValueError("The selected client is offboarded.")
        if key_digest:
            existing = InboundTransfer.objects.select_related("dispatch").filter(idempotency_digest=key_digest).first()
            if existing:
                candidate = stored[0][1]
                if existing.checksum != candidate.checksum or existing.byte_count != candidate.size:
                    raise ValueError("Idempotency key already belongs to different content.")
                # This newly written random object is proven not to be referenced.
                if candidate.key in owned_keys:
                    source_path(candidate.key).unlink(missing_ok=True)
                return [existing], True
        if asynchronous:
            if locked.status != "ACTIVE":
                raise ValueError("The selected client is inactive.")
            pending = InboundTransfer.objects.filter(client=client, dispatch__state__in=["QUEUED", "RUNNING"]).count()
            if pending >= settings.INTAKE_MAX_PENDING_PER_CLIENT:
                raise ValueError("Client intake queue is full. Retry later with the same idempotency key.")
        job_id = uuid.uuid4()
        state = "QUEUED" if asynchronous else "RUNNING"
        job = {"id": str(job_id), "job_type": "INTAKE" if asynchronous else "INLINE_INTAKE",
               "state": state, "scope_key": f"intake:{job_id}", "priority": 0,
               "client_id": str(getattr(client, "pk", "") or ""), "owner_user_id": str(actor_id or ""),
               "system_automation": bool(partner or not actor_id), "started_at": started.isoformat(),
               "worker_started_at": None if asynchronous else started.isoformat(),
               "finished_at": None, "result": None, "workflow": workflow}
        dispatch = BatchDispatch.objects.create(id=job_id, state=state, scope_key=job["scope_key"], priority=0, payload=job)
        receipts = [InboundTransfer.objects.create(
            client=client, actor_id=actor_id, actor_reference=actor_ref, partner=partner,
            channel=channel, workflow=workflow, original_filename=name,
            storage_key=source.key, checksum=source.checksum, byte_count=source.size,
            idempotency_digest=key_digest, received_started_at=started, dispatch=dispatch,
            state="QUEUED" if asynchronous else "RECEIVED",
        ) for name, source in stored]
        if not asynchronous:
            from .intake_lifecycle import start_attempt
            start_attempt(dispatch, job)
            dispatch.payload = job
            dispatch.save(update_fields=["payload", "updated_at"])
    return receipts, False


@transaction.atomic
def finish_inline(receipts, success: bool, *, source_id=None, outcome=None, code=""):
    if not receipts:
        return
    from .batch_jobs import write_job
    job = dict(receipts[0].dispatch.payload)
    BatchDispatch.objects.select_for_update().get(pk=job["id"])
    from .models import ProcessingAttempt
    if job.get("attempt_id") and ProcessingAttempt.objects.filter(
        pk=job["attempt_id"], owner=job["lease_owner"], state__in=["COMPLETED", "FAILED"],
    ).exists():
        return
    from .intake_lifecycle import owned_attempt
    owned_attempt(job, lock=True)
    job["receipt_state"] = outcome or ("COMPLETED" if success else "FAILED")
    job["safe_error_code"] = code or ({"DUPLICATE": "DUPLICATE_FILE", "REJECTED": "VALIDATION_REJECTED"}.get(outcome)
                                       or ("" if success else "PROCESSING_FAILED"))
    InboundTransfer.objects.filter(pk__in=[r.pk for r in receipts], validation_outcome="PENDING").update(
        validation_outcome="VALID" if success or outcome == "DUPLICATE" else ("REJECTED" if outcome == "REJECTED" else "UNRESOLVED"),
    )
    job.update(state="COMPLETED" if success else "FAILED", finished_at=timezone.now().isoformat(),
               result={"success": bool(success), "source_id": str(source_id or "")},
               status_code=200 if success else 400)
    write_job(job)


def text_source(text):
    return io.BytesIO(text.encode("utf-8") if isinstance(text, str) else bytes(text))
