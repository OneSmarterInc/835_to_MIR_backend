"""Lease ownership, common lifecycle, and bounded safe dispatch recovery."""
from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from .models import BatchDispatch, InboundTransfer, ProcessingAttempt

logger = logging.getLogger(__name__)
current_attempt = ContextVar("intake_attempt", default=None)
TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "DUPLICATE"}


class LeaseLost(RuntimeError):
    pass


@lru_cache(maxsize=1)
def converter_version() -> str:
    """Version actual converter code, not an unverified release label."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = [root / "edi835" / name for name in ("services.py", "mir_persistence.py", "edi837_service.py")]
    files += sorted((root / "admin_panel" / "mir_mapper_logic").rglob("*.py"))
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def lease_seconds() -> int:
    return max(30, int(getattr(settings, "INTAKE_LEASE_SECONDS", 300)))


def start_attempt(record: BatchDispatch, job: dict) -> ProcessingAttempt:
    """Caller must hold the dispatch row lock (or have just inserted it)."""
    previous = record.attempts.order_by("-sequence").first()
    now = timezone.now()
    attempt = ProcessingAttempt.objects.create(
        dispatch=record, sequence=previous.sequence + 1 if previous else 1,
        parent=previous, lease_expires_at=now + timedelta(seconds=lease_seconds()),
        converter_version=converter_version(), retry_reason=job.get("retry_reason", "")[:64],
    )
    job.update(attempt_id=str(attempt.pk), lease_owner=str(attempt.owner))
    job.update(safe_error_code="", result=None, finished_at=None, status_code=None)
    job.pop("intake_result", None)
    job.pop("retryable", None)
    record.transfers.exclude(state__in=TERMINAL).update(
        state="VALIDATING", safe_error_code="", validation_outcome="PENDING",
        converter_version=attempt.converter_version, updated_at=now,
    )
    return attempt


def owned_attempt(job: dict, *, lock=False) -> ProcessingAttempt | None:
    if not job.get("attempt_id"):
        return None
    query = ProcessingAttempt.objects.select_for_update() if lock else ProcessingAttempt.objects
    attempt = query.filter(pk=job["attempt_id"], dispatch_id=job["id"], owner=job.get("lease_owner"),
                           state="RUNNING", lease_expires_at__gt=timezone.now()).first()
    if attempt is None:
        raise LeaseLost("Processing lease is no longer owned by this execution.")
    return attempt


def renew(job: dict) -> bool:
    now = timezone.now()
    return bool(ProcessingAttempt.objects.filter(
        pk=job.get("attempt_id"), owner=job.get("lease_owner"), state="RUNNING",
        lease_expires_at__gt=now,
    ).update(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds())))


@contextmanager
def execution(job: dict):
    """Heartbeat uses its own connection; ownership is checked at effect boundaries."""
    owned_attempt(job)
    token = current_attempt.set(job)
    stopped = threading.Event()

    def heartbeat():
        try:
            while not stopped.wait(lease_seconds() / 3):
                close_old_connections()
                try:
                    if not renew(job):
                        return
                except Exception:
                    # No claim identifiers or database exception contents in logs.
                    logger.error("intake_lease_renewal_failed attempt=%s", job.get("attempt_id"))
        finally:
            close_old_connections()

    # Uncommitted rows are invisible to another connection. Production adapters
    # must start execution outside request-wide transactions. Tests use TestCase.
    thread = None
    if job.get("attempt_id") and not connection.in_atomic_block:
        thread = threading.Thread(target=heartbeat, daemon=True, name="intake-lease")
        thread.start()
    try:
        yield
    finally:
        stopped.set()
        if thread:
            thread.join(timeout=2)
        current_attempt.reset(token)


def begin_effects() -> None:
    """Durably fence automatic replay before legacy database/filesystem/SFTP effects."""
    job = current_attempt.get()
    if not job:
        return
    with transaction.atomic():
        BatchDispatch.objects.select_for_update().get(pk=job["id"])
        attempt = owned_attempt(job, lock=True)
        if attempt.effects_started_at is None:
            attempt.effects_started_at = timezone.now()
            attempt.save(update_fields=["effects_started_at"])
        InboundTransfer.objects.filter(dispatch_id=job["id"]).exclude(state__in=TERMINAL).update(
            state="PROCESSING", updated_at=timezone.now(),
        )


@transaction.atomic
def record_validation(receipt, raw: bytes, *, outcome="PENDING", code="") -> None:
    # X12 defines the separators in ISA; counting ST does not imply validation.
    text = raw.decode("utf-8-sig", errors="replace").lstrip()
    count = None
    if text.startswith("ISA") and len(text) > 105:
        element, segment = text[3], text[105]
        count = sum(part.lstrip().startswith("ST" + element) for part in text.split(segment))
    job = current_attempt.get()
    if job:
        BatchDispatch.objects.select_for_update().get(pk=job["id"])
        owned_attempt(job)
    InboundTransfer.objects.filter(pk=receipt.pk).update(
        transaction_set_count=count, validation_outcome=outcome, safe_error_code=code,
        converter_version=converter_version(), updated_at=timezone.now(),
    )
    from .intake_provenance import record_transactions
    record_transactions(receipt, raw)


@transaction.atomic
def record_batch_validation(filename: str, raw_text: str, valid: bool) -> None:
    """Record per-source outcomes; equal names alone never identify an input."""
    from .intake_service import current_transfer
    from .intake_storage import read_verified
    receipt = current_transfer.get()
    if not receipt:
        return
    candidates = InboundTransfer.objects.filter(dispatch_id=receipt.dispatch_id, original_filename=filename)
    for candidate in candidates:
        raw = read_verified(candidate)
        if raw.decode("utf-8-sig").strip() != raw_text.lstrip("\ufeff").strip():
            continue
        record_validation(candidate, raw, outcome="VALID" if valid else "REJECTED",
                          code="" if valid else "VALIDATION_REJECTED")
        if not valid:
            InboundTransfer.objects.filter(pk=candidate.pk).update(state="REJECTED", updated_at=timezone.now())


def finish_attempt(record: BatchDispatch, job: dict) -> None:
    """Called under dispatch lock by write_job; stale workers cannot finish."""
    attempt = owned_attempt(job, lock=True)
    if attempt is None or job["state"] == "RUNNING":
        return
    success = job["state"] == "COMPLETED"
    result = job.get("intake_result") or job.get("result") or {}
    if success:
        from .intake_outputs import verify_dispatch_outputs
        verify_dispatch_outputs(record.pk)
        source_id = result.get("source_id") or result.get("file_id")
        if source_id and record.transfers.filter(workflow="835").exists():
            from .models import EDI835File
            source = EDI835File.objects.filter(pk=source_id).first()
            if source and not record.outputs.filter(source_835=source).exists():
                raise ValueError("Conversion cannot complete without a durable MIR checkpoint.")
    attempt.state = "COMPLETED" if success else "FAILED"
    attempt.finished_at = timezone.now()
    attempt.safe_error_code = job.get("safe_error_code") or ("" if success else "PROCESSING_FAILED")
    # Persist only safe identifiers and outcome; legacy result may contain PHI.
    attempt.result = {"success": success, "source_id": str(result.get("source_id") or result.get("file_id") or "")}
    attempt.save(update_fields=["state", "finished_at", "safe_error_code", "result"])
    state = job.get("receipt_state") or ("COMPLETED" if success else "FAILED")
    if job["state"] == "QUEUED":
        state = "QUEUED"
    record.transfers.exclude(state__in=["REJECTED", "DUPLICATE"]).update(
        state=state, safe_error_code=attempt.safe_error_code,
        source_reference=attempt.result["source_id"][:100], updated_at=timezone.now(),
    )
    if record.transfers.exists():
        from .intake_receipts import receipt_summary
        summaries = [receipt_summary(receipt) for receipt in record.transfers.all()]
        job["receipts"] = summaries
        if isinstance(job.get("result"), dict):
            job["result"]["receipts"] = summaries


def recover_expired(*, apply: bool = False) -> dict:
    """Only work with no recorded effects is safe for automatic replay."""
    result = {"expired": 0, "requeued": 0, "review_required": 0, "outputs_recovered": 0}
    ids = ProcessingAttempt.objects.filter(state="RUNNING", lease_expires_at__lte=timezone.now()).values_list("dispatch_id", flat=True)
    for identifier in ids.iterator(chunk_size=100):
        with transaction.atomic():
            dispatch = BatchDispatch.objects.select_for_update().get(pk=identifier)
            attempt = dispatch.attempts.select_for_update().filter(state="RUNNING", lease_expires_at__lte=timezone.now()).first()
            if not attempt:
                continue
            result["expired"] += 1
            job = dict(dispatch.payload)
            outputs = list(dispatch.outputs.select_related("mir_file", "source_835"))
            durable = bool(outputs) and dispatch.transfers.exists() and not dispatch.transfers.exclude(workflow="835").exists()
            if durable:
                from .intake_outputs import verify_output
                try:
                    for output in outputs:
                        verify_output(output)
                except (OSError, ValueError):
                    durable = False
            if durable:
                result["outputs_recovered"] += 1
                if apply:
                    now = timezone.now()
                    # This completes local conversion only. NOT_STARTED/SENDING
                    # remains visible; recovery never sends to a remote partner.
                    outcome = {"success": True, "source_id": str(outputs[0].source_835_id), "recovered_output": True}
                    attempt.state, attempt.finished_at, attempt.result = "COMPLETED", now, outcome
                    attempt.save(update_fields=["state", "finished_at", "result"])
                    job.update(state="COMPLETED", finished_at=now.isoformat(), result=outcome, intake_result=outcome)
                    dispatch.state, dispatch.payload = "COMPLETED", job
                    dispatch.save(update_fields=["state", "payload", "updated_at"])
                    dispatch.transfers.exclude(state__in=["REJECTED", "DUPLICATE"]).update(
                        state="COMPLETED", safe_error_code="", source_reference=outcome["source_id"], updated_at=now,
                    )
                    for output in outputs:
                        source = output.source_835
                        source.status = "ARCHIVED"
                        source.processing_completed_at = now
                        source.present_in_archive_folder = True
                        source.present_in_sftp = output.delivery_state == "CONFIRMED"
                        source.error_message = None
                        source.save(update_fields=["status", "processing_completed_at", "present_in_archive_folder", "present_in_sftp", "error_message"])
                continue
            safe = (job.get("job_type") == "INTAKE" and attempt.effects_started_at is None
                    and attempt.sequence < int(getattr(settings, "INTAKE_MAX_ATTEMPTS", 3)))
            result["requeued" if safe else "review_required"] += 1
            if not apply:
                continue
            now = timezone.now()
            attempt.state, attempt.finished_at = "ABANDONED", now
            attempt.safe_error_code = "LEASE_EXPIRED" if safe else "RECOVERY_REVIEW_REQUIRED"
            attempt.save(update_fields=["state", "finished_at", "safe_error_code"])
            job.update(state="QUEUED" if safe else "FAILED", retry_reason="LEASE_EXPIRED",
                       safe_error_code=attempt.safe_error_code, finished_at=None if safe else now.isoformat())
            if safe:
                job["not_before"] = (now + timedelta(seconds=min(300, 10 * 2 ** (attempt.sequence - 1)))).isoformat()
            else:
                job["result"] = {"success": False, "error": "Execution interrupted; verify output and delivery before retrying."}
                if job.get("job_type") == "INTAKE":
                    job["intake_result"] = job["result"]
            job.pop("attempt_id", None)
            job.pop("lease_owner", None)
            dispatch.state, dispatch.payload = job["state"], job
            dispatch.save(update_fields=["state", "payload", "updated_at"])
            dispatch.transfers.exclude(state__in=["REJECTED", "DUPLICATE"]).update(
                state=dispatch.state, safe_error_code=attempt.safe_error_code, updated_at=now,
            )
    return result
