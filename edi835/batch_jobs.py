"""Small durable file-backed queue for resource-heavy batch conversions.

Jobs intentionally live outside Gunicorn memory.  A separate systemd worker
claims them and writes status atomically so web restarts do not lose progress.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone


def jobs_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / "edi835" / "batch_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(job_id: str) -> Path:
    # UUID parsing prevents path traversal and gives callers one canonical key.
    import uuid
    return jobs_dir() / f"{uuid.UUID(str(job_id))}.json"


def read_job(job_id: str) -> dict | None:
    try:
        with _path(job_id).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def write_job(job: dict) -> None:
    path = _path(job["id"])
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(job, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def queued_jobs() -> list[dict]:
    jobs = []
    for path in jobs_dir().glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                job = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("state") == "QUEUED":
            not_before = job.get("not_before")
            if not_before:
                try:
                    if datetime.fromisoformat(not_before) > timezone.now():
                        continue
                except (TypeError, ValueError):
                    pass
            jobs.append(job)
    return sorted(jobs, key=lambda item: item.get("started_at") or "")


def active_job_for(scope_key: str) -> dict | None:
    for path in jobs_dir().glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                job = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("scope_key") == scope_key and job.get("state") in {"QUEUED", "RUNNING"}:
            return job
    return None


def recover_interrupted_jobs() -> int:
    """Mark jobs abandoned by a killed/restarted worker as failed."""
    recovered = 0
    for path in jobs_dir().glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                job = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("state") != "RUNNING":
            continue
        job["state"] = "FAILED"
        job["finished_at"] = timezone.now().isoformat()
        job["status_code"] = 500
        job["result"] = {
            "success": False,
            "error": "The isolated batch worker stopped before this conversion completed. The inbound files were retained for a safe retry.",
        }
        write_job(job)
        recovered += 1
    return recovered
