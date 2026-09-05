"""Directional SFTP operations used by the persistent scheduler."""

import io
import os
import posixpath
import stat
import uuid
from pathlib import Path

from django.utils import timezone

from .admin_sftp_routes import resolve_admin_sftp_route
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .models import EDI835File, EDI837File, MIRFile
from .services import process_multiple_edi835_files, validate_835_content
from .storage import archive_inbound, client_storage_dirs, relative_media_path, remove_delivered_outbound, stage_inbound


def _connected(client, purpose, outbound=False):
    """Return the exact administrator-configured connection and folder."""
    config, credentials, _folder = resolve_admin_sftp_route(client, purpose)
    return config, credentials


def ingest_835_incoming(client, actor):
    """Validate, persist and archive inbound 835 files without converting them."""
    _config, credentials = _connected(client, "835_IN")
    import paramiko
    ssh = sftp = None
    taken, errors = [], []
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        folder = _normalize_folder(sftp, credentials["remote_folder"])
        entries = sftp.listdir_attr(folder)
        for entry in sorted(entries, key=lambda item: item.filename):
            name = entry.filename
            if stat.S_ISDIR(entry.st_mode) or name.startswith(".") or not has_valid_file_extension(name, "835"):
                continue
            remote_path = posixpath.join(folder, name)
            try:
                with sftp.open(remote_path, "rb") as handle:
                    raw = handle.read()
                text = raw.decode("utf-8-sig", errors="replace").strip()
                valid, report = validate_835_content(text)
                if not valid:
                    detail = "835 validation failed: " + "; ".join(report.get("errors") or ["invalid content"])
                    stored = f"{uuid.uuid4().hex}_{os.path.basename(name)}"
                    inbound = stage_inbound(client, "835", stored, raw, binary=True)
                    archived = archive_inbound(client, "835", inbound)
                    EDI835File.objects.create(
                        client=client, original_filename=name, stored_filename=stored,
                        input_file_content=text, status="ERROR", archive_path=relative_media_path(archived),
                        present_in_sftp=False, present_in_archive_folder=True, ingestion_source="SFTP",
                        error_message=detail, processing_completed_at=timezone.now(),
                    )
                    sftp.remove(remote_path)
                    errors.append(f"{name}: {detail}")
                    continue
                stored = f"{uuid.uuid4().hex}_{os.path.basename(name)}"
                inbound = stage_inbound(client, "835", stored, raw, binary=True)
                archived = archive_inbound(client, "835", inbound)
                EDI835File.objects.create(
                    client=client, original_filename=name, stored_filename=stored,
                    input_file_content=text, status="UPLOADED", archive_path=relative_media_path(archived),
                    present_in_sftp=False, present_in_archive_folder=True, ingestion_source="SFTP",
                )
                sftp.remove(remote_path)
                taken.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    finally:
        if sftp: sftp.close()
        if ssh: ssh.close()
    return {"success": not errors or bool(taken), "automation_type": "835", "direction": "INCOMING",
            "files": taken, "processed_count": len(taken), "errors": errors,
            "message": f"Validated and archived {len(taken)} inbound 835 file(s)."}


def process_staged_835(client):
    records = list(EDI835File.objects.filter(client=client, status="UPLOADED").order_by("uploaded_at")[:500])
    items = [{"filename": item.original_filename, "content": item.input_file_content} for item in records if item.input_file_content]
    if not items:
        return {"success": True, "automation_type": "835", "direction": "PROCESSING", "files": [],
                "processed_count": 0, "message": "No validated 835 files were waiting for processing."}
    result = process_multiple_edi835_files(items, ingestion_source="SFTP", client=client, deliver_outbound=False)
    if result.get("success"):
        EDI835File.objects.filter(id__in=[item.id for item in records]).update(
            status="ARCHIVED", processing_completed_at=timezone.now())
    # Service-layer results include live Django objects for synchronous callers.
    # Durable worker jobs are JSON, so expose identifiers and names instead of
    # attempting to serialize a model instance (and omit the large MIR body).
    generated_record = result.pop("db_record", None)
    result.pop("mir_text", None)
    if generated_record is not None:
        result["edi835_file_id"] = str(generated_record.id)
        mir_file = getattr(generated_record, "mir_file", None)
        if mir_file is not None:
            result["mir_file_id"] = str(mir_file.id)
            result["mir_filename"] = mir_file.mir_filename or ""
    if not result.get("mir_filename"):
        result["mir_filename"] = result.get("combined_filename") or ""
    result.update({"automation_type": "835", "direction": "PROCESSING", "files": [item.original_filename for item in records],
                   "processed_count": len(records) if result.get("success") else 0})
    return result


def push_local_outbound(client, kind):
    kind = kind.lower()
    purpose = "837_OUT" if kind == "837" else "MIR_OUT"
    _config, credentials = _connected(client, purpose, outbound=True)
    directory = client_storage_dirs(client)[f"{kind}_out"]
    import paramiko
    ssh = sftp = None
    sent, errors = [], []
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        folder = _normalize_folder(sftp, credentials["remote_folder"])
        existing = set(sftp.listdir(folder))
        for local_path in sorted(path for path in directory.iterdir() if path.is_file() and not path.name.startswith(".")):
            target = posixpath.join(folder, local_path.name)
            temporary = posixpath.join(folder, f".{local_path.name}.{uuid.uuid4().hex}.uploading")
            if local_path.name in existing:
                errors.append(f"{local_path.name}: already exists in outbound SFTP; local file retained")
                continue
            try:
                with local_path.open("rb") as source:
                    sftp.putfo(source, temporary, file_size=local_path.stat().st_size, confirm=True)
                sftp.rename(temporary, target)
                remove_delivered_outbound(client, kind, local_path)
                sent.append(local_path.name)
                if kind == "837":
                    EDI837File.objects.filter(client=client, outbound_path__endswith=local_path.name).update(outbound_path=target)
                else:
                    MIRFile.objects.filter(client=client, mir_filename=local_path.name).update(status="PUSHED", updated_at=timezone.now())
            except Exception as exc:
                try: sftp.remove(temporary)
                except Exception: pass
                errors.append(f"{local_path.name}: {exc}")
    finally:
        if sftp: sftp.close()
        if ssh: ssh.close()
    label = kind.upper()
    return {"success": not errors or bool(sent), "automation_type": label, "direction": "OUTGOING",
            "sent_files": sent, "processed_count": len(sent), "errors": errors,
            "message": f"Sent {len(sent)} {label} outbound file(s)."}


def execute_directional_operation(client, actor, automation_type, direction):
    key = (automation_type.upper(), direction.upper())

    # Manual Conversion -> Test uses automation_type=ALL. It is not a single
    # directional scheduler operation; returning None tells the worker to run
    # the existing complete batch pipeline instead.
    if key[0] == "ALL":
        return None

    if key == ("835", "INCOMING"):
        return ingest_835_incoming(client, actor)
    if key == ("835", "PROCESSING"):
        return process_staged_835(client)
    if key == ("837", "OUTGOING"):
        return push_local_outbound(client, "837")
    if key == ("MIR", "OUTGOING"):
        return push_local_outbound(client, "mir")
    if key in {("837", "INCOMING"), ("RECON", "INCOMING")}:
        return None
    raise ValueError("Unsupported SFTP automation operation.")
