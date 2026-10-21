"""Conservative reconciliation. Uncertain delivery is never resent here."""
import json
import hashlib
import os
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from edi835.intake_lifecycle import recover_expired
from edi835.intake_outputs import verify_output
from edi835.intake_storage import _sync_directory, read_verified, source_path, storage_root
from edi835.models import BatchDispatch, InboundTransfer, IntakeOutput


def quarantine_object(path: Path, root: Path, key: str, origin: str) -> None:
    target = root / ".quarantine" / uuid.uuid4().hex
    target.mkdir(parents=True, mode=0o700)
    with (target / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump({"original_key": key, "origin": origin, "quarantined_at": timezone.now().isoformat()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    # Media and private storage can live on different volumes. Copy, fsync,
    # verify, then unlink. A crash may retain two copies, never zero copies.
    with path.open("rb") as source, (target / "object").open("xb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
        destination.flush()
        os.fsync(destination.fileno())
    with path.open("rb") as source, (target / "object").open("rb") as copy:
        if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(copy, "sha256").digest():
            raise CommandError("Quarantine verification failed; the original was retained.")
    _sync_directory(target)
    _sync_directory(target.parent)
    _sync_directory(root)
    path.unlink()
    _sync_directory(path.parent)


class Command(BaseCommand):
    help = "Audit intake; --apply repairs safe dispatch/lifecycle gaps. Orphan quarantine requires stopped producers."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--verify-hashes", action="store_true")
        parser.add_argument("--grace-hours", type=int, default=24)
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--after-receipt", type=uuid.UUID)
        parser.add_argument("--scan-media-temporaries", action="store_true",
                            help="Also inspect old atomic-write .tmp files in MEDIA_ROOT.")
        parser.add_argument("--quarantine-orphans", action="store_true")
        parser.add_argument("--producers-stopped", action="store_true",
                            help="Operator assertion: web, workers, SFTP and API producers have been stopped.")

    def handle(self, *args, **options):
        apply = options["apply"]
        quarantine = options["quarantine_orphans"]
        if quarantine and not (apply and options["producers_stopped"]):
            raise CommandError("Quarantine requires --apply --producers-stopped after stopping all intake producers.")
        if quarantine and BatchDispatch.objects.filter(state__in=["QUEUED", "RUNNING"]).exists():
            raise CommandError("Drain or reconcile active dispatches before orphan quarantine.")
        if options["limit"] < 1 or options["grace_hours"] < 1:
            raise CommandError("Limit and grace hours must be positive.")
        report = {"applied": apply, "leases": recover_expired(apply=apply), "invalid_sources": [],
                  "unqueued_receipts": [], "invalid_outputs": [], "delivery_review": [],
                  "orphan_candidates": [], "media_temporary_count": 0, "quarantined": 0, "next_after_receipt": None}
        query = InboundTransfer.objects.order_by("id")
        if options["after_receipt"]:
            query = query.filter(pk__gt=options["after_receipt"])
        for receipt in query[:options["limit"]]:
            report["next_after_receipt"] = str(receipt.pk)
            valid = True
            try:
                if options["verify_hashes"]:
                    read_verified(receipt)
                elif source_path(receipt.storage_key).stat().st_size != receipt.byte_count:
                    valid = False
            except (OSError, ValueError):
                valid = False
            if not valid:
                report["invalid_sources"].append(str(receipt.pk))
            with transaction.atomic():
                dispatch = BatchDispatch.objects.select_for_update().get(pk=receipt.dispatch_id)
                live = dispatch.attempts.filter(state="RUNNING", lease_expires_at__gt=timezone.now()).exists()
                if not valid:
                    if apply and not live:
                        receipt.state, receipt.safe_error_code = "FAILED", "SOURCE_INTEGRITY_FAILED"
                        receipt.save(update_fields=["state", "safe_error_code", "updated_at"])
                        if dispatch.state == "QUEUED":
                            dispatch.state = "FAILED"
                            dispatch.payload = {**dispatch.payload, "state": "FAILED", "safe_error_code": "SOURCE_INTEGRITY_FAILED"}
                            dispatch.save(update_fields=["state", "payload", "updated_at"])
                    continue
                # A FK prevents receipts without a dispatch. Repair stranded
                # pre-execution intents only; never rerun an executed operation.
                if (receipt.state in {"RECEIVED", "QUEUED"} and dispatch.state != "QUEUED"
                        and dispatch.payload.get("job_type") == "INTAKE" and not dispatch.attempts.exists()):
                    report["unqueued_receipts"].append(str(receipt.pk))
                    if apply:
                        dispatch.state = "QUEUED"
                        dispatch.payload = {**dispatch.payload, "state": "QUEUED", "retry_reason": "RECONCILED_INTENT"}
                        dispatch.save(update_fields=["state", "payload", "updated_at"])
                        receipt.state = "QUEUED"
                        receipt.save(update_fields=["state", "updated_at"])
        for output in IntakeOutput.objects.select_related("mir_file").iterator(chunk_size=100):
            try:
                verify_output(output)
            except (OSError, ValueError):
                report["invalid_outputs"].append(str(output.pk))
                if apply:
                    with transaction.atomic():
                        dispatch = BatchDispatch.objects.select_for_update().get(pk=output.dispatch_id)
                        if not dispatch.attempts.filter(state="RUNNING").exists():
                            dispatch.transfers.update(state="FAILED", safe_error_code="OUTPUT_INTEGRITY_FAILED", updated_at=timezone.now())
                            dispatch.state = "FAILED"
                            dispatch.payload = {**dispatch.payload, "state": "FAILED", "safe_error_code": "OUTPUT_INTEGRITY_FAILED"}
                            dispatch.save(update_fields=["state", "payload", "updated_at"])
            if output.delivery_state in {"SENDING", "UNCONFIRMED"}:
                report["delivery_review"].append(str(output.pk))
        cutoff = timezone.now() - timedelta(hours=options["grace_hours"])
        root = storage_root()
        for path in root.glob("*/*"):
            if len(report["orphan_candidates"]) >= options["limit"]:
                break
            if (path.is_symlink() or path.parent.is_symlink() or path.parent.name.startswith(".")
                    or not path.is_file() or path.stat().st_mtime >= cutoff.timestamp()):
                continue
            if path.suffix not in {".bin", ".part"}:
                continue
            key = path.relative_to(root).as_posix()
            if InboundTransfer.objects.filter(storage_key=key).exists() or IntakeOutput.objects.filter(storage_key=key).exists():
                continue
            report["orphan_candidates"].append(key)
            if quarantine:
                quarantine_object(path, root, key, "private")
                report["quarantined"] += 1
        if options["scan_media_temporaries"]:
            media = Path(settings.MEDIA_ROOT).resolve()
            for path in media.rglob(".*.tmp"):
                if report["media_temporary_count"] >= options["limit"]:
                    break
                if (not re.fullmatch(r"\..+\.[0-9a-f]{32}\.tmp", path.name)
                        or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
                        or not path.is_file() or media not in path.resolve().parents
                        or path.stat().st_mtime >= cutoff.timestamp()):
                    continue
                report["media_temporary_count"] += 1
                if quarantine:
                    quarantine_object(path, root, path.relative_to(media).as_posix(), "media")
                    report["quarantined"] += 1
        self.stdout.write(json.dumps(report, indent=2))
