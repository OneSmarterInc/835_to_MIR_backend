"""Bounded, read-only inspection of existing queue files; no payloads in output."""
import json
from datetime import datetime
from pathlib import Path


def _age(value, now: datetime) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return None
        seconds = (now - timestamp).total_seconds()
        return round(seconds, 3) if seconds >= 0 else None
    except (ValueError, OverflowError):
        return None


def observe_queue(path: Path, now: datetime, *, max_files: int = 5000,
                  max_bytes: int = 1024 * 1024) -> dict:
    if now.tzinfo is None or max_files < 1 or max_bytes < 1:
        raise ValueError("An aware timestamp and positive scan limits are required.")
    result = {
        "directory_present": path.is_dir(), "scan_complete": True,
        "scanned_files": 0, "unreadable_files": 0, "invalid_active_timestamps": 0,
        "counts": {state: 0 for state in ("QUEUED", "RUNNING", "COMPLETED", "FAILED")},
        "oldest_queued_seconds": None, "oldest_running_seconds": None,
        "note": "Instantaneous queue snapshot, not a worker heartbeat. Queued age includes delayed retries.",
    }
    if not result["directory_present"]:
        result["scan_complete"] = False
        return result
    try:
        for item in path.iterdir():
            if item.suffix != ".json":
                continue
            if result["scanned_files"] >= max_files:
                result["scan_complete"] = False
                break
            result["scanned_files"] += 1
            try:
                if item.is_symlink() or not item.is_file():
                    raise ValueError("Not a regular queue file")
                with item.open("rb") as handle:
                    raw = handle.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ValueError("Queue file exceeds scan bound")
                job = json.loads(raw)
                if not isinstance(job, dict) or job.get("state") not in result["counts"]:
                    raise ValueError("Invalid queue state")
            except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
                result["unreadable_files"] += 1
                result["scan_complete"] = False
                continue
            state = job["state"]
            result["counts"][state] += 1
            if state in {"QUEUED", "RUNNING"}:
                key = "started_at" if state == "QUEUED" else "worker_started_at"
                age = _age(job.get(key), now)
                if age is None:
                    result["invalid_active_timestamps"] += 1
                    result["scan_complete"] = False
                    continue
                target = "oldest_queued_seconds" if state == "QUEUED" else "oldest_running_seconds"
                previous = result[target]
                result[target] = age if previous is None else max(previous, age)
    except OSError:
        result["scan_complete"] = False
    return result
