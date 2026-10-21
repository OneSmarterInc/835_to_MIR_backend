"""Database dispatch queue. Writes participate in the caller's transaction."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import BatchDispatch

MANUAL_CONVERSION_PRIORITY = 0
DEFAULT_JOB_PRIORITY = 20
logger = logging.getLogger(__name__)


def jobs_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / "edi835" / "batch_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(job_id: str) -> Path:
    return jobs_dir() / f"{uuid.UUID(str(job_id))}.json"


def import_legacy_jobs() -> int:
    """Non-destructive adoption on worker startup; DB state always wins."""
    count = 0
    for path in jobs_dir().glob("*.json"):
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                logger.warning("Oversize legacy queue file was not imported; inspect the legacy queue before rollout.")
                continue
            job = json.loads(path.read_text(encoding="utf-8"))
            if str(uuid.UUID(str(job["id"]))) != path.stem:
                logger.warning("Legacy queue filename and job ID disagree; file was not imported.")
                continue
            fields = _fields(job)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Unreadable or malformed legacy queue file was not imported.")
            continue
        _, created = BatchDispatch.objects.get_or_create(id=job["id"], defaults=fields)
        if created and job.get("started_at"):
            try:
                started = datetime.fromisoformat(job["started_at"])
                if timezone.is_naive(started):
                    started = timezone.make_aware(started)
                BatchDispatch.objects.filter(pk=job["id"]).update(created_at=started)
            except (ValueError, TypeError):
                pass
        count += int(created)
    return count


def read_job(job_id: str) -> dict | None:
    try:
        identifier = uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        return None
    record = BatchDispatch.objects.filter(pk=identifier).only("payload").first()
    if record:
        return record.payload
    try:
        job = json.loads(_path(str(identifier)).read_text(encoding="utf-8"))
        if str(job.get("id")) == str(identifier):
            return job
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def job_priority(job: dict) -> int:
    if job.get("job_type") == "MANUAL_CONVERSION":
        return MANUAL_CONVERSION_PRIORITY
    try:
        return int(job.get("priority", DEFAULT_JOB_PRIORITY))
    except (TypeError, ValueError):
        return DEFAULT_JOB_PRIORITY


def _fields(job):
    if job.get("state") not in {"QUEUED", "RUNNING", "COMPLETED", "FAILED"}:
        raise ValueError("Invalid dispatch state.")
    return {"state": job["state"], "scope_key": job.get("scope_key") or "",
            "priority": job_priority(job), "payload": job}


def write_job(job: dict) -> None:
    from .intake_lifecycle import LeaseLost, finish_attempt
    with transaction.atomic():
        record = BatchDispatch.objects.select_for_update().filter(pk=job["id"]).first()
        if record is None:
            if job.get("attempt_id"):
                raise LeaseLost("Dispatch no longer exists.")
            BatchDispatch.objects.create(id=uuid.UUID(str(job["id"])), **_fields(job))
            return
        if record.attempts.exists() and not job.get("attempt_id"):
            raise LeaseLost("An execution token is required to update this dispatch.")
        finish_attempt(record, job)
        for key, value in _fields(job).items():
            setattr(record, key, value)
        record.save(update_fields=["state", "scope_key", "priority", "payload", "updated_at"])


def claim_job(job_id: str) -> dict | None:
    """Claim a still-queued job in a short transaction, then release the lock."""
    with transaction.atomic():
        record = BatchDispatch.objects.select_for_update().filter(pk=job_id, state="QUEUED").first()
        if record is None:
            return None
        job = dict(record.payload)
        if job.get("not_before"):
            try:
                due = datetime.fromisoformat(job["not_before"])
                if timezone.is_naive(due):
                    due = timezone.make_aware(due)
                if due > timezone.now():
                    return None
            except (ValueError, TypeError):
                return None
        job.update(state="RUNNING", attempt_count=int(job.get("attempt_count") or 0) + 1,
                   worker_started_at=timezone.now().isoformat())
        from .intake_lifecycle import start_attempt
        start_attempt(record, job)
        record.state, record.payload = "RUNNING", job
        record.save(update_fields=["state", "payload", "updated_at"])
        return job


def queued_jobs() -> list[dict]:
    ready = []
    for job in BatchDispatch.objects.filter(state="QUEUED").order_by("priority", "created_at").values_list("payload", flat=True).iterator(chunk_size=100):
        if job.get("not_before"):
            try:
                due = datetime.fromisoformat(job["not_before"])
                if timezone.is_naive(due):
                    due = timezone.make_aware(due)
                if due > timezone.now():
                    continue
            except (ValueError, TypeError):
                continue
        ready.append(job)
        if len(ready) >= 100:
            break
    return ready


def active_job_for(scope_key: str) -> dict | None:
    record = BatchDispatch.objects.filter(scope_key=scope_key, state__in=["QUEUED", "RUNNING"]).only("payload").first()
    return record.payload if record else None


def prune_finished_jobs(max_age_days: int = 7) -> int:
    cutoff = timezone.now() - timedelta(days=max(1, int(max_age_days)))
    # Receipts and dispatch history need an approved retention policy.
    # Do not prune referenced dispatches or resurrect legacy JSON entries.
    legacy_ids = [p.stem for p in jobs_dir().glob("*.json") if _valid_uuid(p.stem)]
    records = BatchDispatch.objects.filter(state__in=["COMPLETED", "FAILED"], updated_at__lt=cutoff, transfers__isnull=True, attempts__isnull=True).exclude(pk__in=legacy_ids)
    count, _ = records.delete()
    return count


def _valid_uuid(value):
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def recover_interrupted_jobs() -> int:
    import_legacy_jobs()
    from .intake_lifecycle import recover_expired
    recovered = recover_expired(apply=True)["expired"]
    for job in BatchDispatch.objects.filter(state="RUNNING", attempts__isnull=True).values_list("payload", flat=True).iterator():
        # Inline processing belongs to web/SFTP callers, not this worker.
        if job.get("job_type") == "INLINE_INTAKE":
            continue
        job.update(state="FAILED", finished_at=timezone.now().isoformat(), status_code=500,
                   result={"success": False, "error": "Worker interrupted. Review delivery before retrying; source files are retained."})
        if job.get("job_type") == "INTAKE":
            job["intake_result"] = job["result"]
        write_job(job)
        recovered += 1
    return recovered
