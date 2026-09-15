"""Directional SFTP operations used by the persistent scheduler."""

import io
import json
import os
import posixpath
import stat
import uuid
from pathlib import Path

from django.utils import timezone

from .admin_sftp_routes import resolve_admin_sftp_route
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .mir_persistence import set_mir_push_status
from .models import EDI835File, EDI837File, MIRFile
from .services import process_edi835_file_content, validate_835_content
from .storage import archive_inbound, client_storage_dirs, relative_media_path, remove_delivered_outbound, stage_inbound


def _connected(client, purpose, outbound=False):
    """Return the exact administrator-configured connection and folder."""
    config, credentials, _folder = resolve_admin_sftp_route(client, purpose)
    return config, credentials


def _serialize_validation_error(report):
    """Return one durable JSON shape for refused inbound 835 validation."""
    report = report if isinstance(report, dict) else {}
    payload = {
        "type": "835_validation_error",
        "decision": report.get("decision") or "REFUSE",
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
        "findings": report.get("findings") or [],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _append_hhss(filename, now=None):
    """Append hour+seconds immediately before the extension for outbound names."""
    stamp = timezone.localtime(now or timezone.now()).strftime("%H%S")
    safe_name = os.path.basename(filename)
    stem, extension = os.path.splitext(safe_name)
    return f"{stem}_{stamp}{extension}"


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
                    detail = _serialize_validation_error(report)
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
                    errors.append(f"{name}: 835 validation failed")
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
    """Process validated SFTP 835 records strictly one at a time.

    A failed file does not prevent later files in a 30+ file batch from being
    attempted, and each successful result is persisted before moving on.
    """
    records = list(
        EDI835File.objects.filter(client=client, status="UPLOADED")
        .exclude(input_file_content="")
        .order_by("uploaded_at")[:500]
    )
    if not records:
        return {"success": True, "automation_type": "835", "direction": "PROCESSING", "files": [],
                "processed_count": 0, "errors": [],
                "message": "No validated 835 files were waiting for processing."}

    processed, errors, outputs = [], [], []
    for record in records:
        try:
            result = process_edi835_file_content(
                record.input_file_content,
                original_filename=record.original_filename or record.stored_filename or "file.835",
                file_id=record.id,
                ingestion_source="SFTP",
                client=client,
            )
            result.pop("mir_text", None)
            generated_record = result.pop("db_record", None)
            if not result.get("success"):
                errors.append(f"{record.original_filename}: {result.get('error') or 'conversion failed'}")
                continue

            record.refresh_from_db()
            processed.append(record.original_filename)
            mir_file = getattr(generated_record or record, "mir_file", None)
            if mir_file is not None and mir_file.mir_filename:
                outputs.append(mir_file.mir_filename)
        except Exception as exc:
            errors.append(f"{record.original_filename}: {exc}")

    return {
        "success": bool(processed) or not errors,
        "automation_type": "835",
        "direction": "PROCESSING",
        "files": [record.original_filename for record in records],
        "processed_files": processed,
        "processed_count": len(processed),
        "mir_filename": outputs[-1] if outputs else "",
        "mir_filenames": outputs,
        "errors": errors,
        "message": f"Processed {len(processed)} of {len(records)} staged 835 file(s) one by one.",
    }


def push_local_outbound(client, kind):
    """Send outbound files one at a time and verify each remote object."""
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
        local_files = sorted(
            path for path in directory.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
        for local_path in local_files:
            remote_name = _append_hhss(local_path.name)
            target = posixpath.join(folder, remote_name)
            temporary = posixpath.join(folder, f".{remote_name}.{uuid.uuid4().hex}.uploading")
            if remote_name in existing:
                errors.append(f"{remote_name}: already exists in outbound SFTP; local file retained")
                continue
            try:
                with local_path.open("rb") as source:
                    sftp.putfo(source, temporary, file_size=local_path.stat().st_size, confirm=True)
                sftp.rename(temporary, target)
                # Do not delete the local queue copy until the remote file can
                # actually be stat'ed after the atomic rename.
                sftp.stat(target)
                remove_delivered_outbound(client, kind, local_path)
                sent.append(remote_name)
                existing.add(remote_name)
                if kind == "837":
                    EDI837File.objects.filter(
                        client=client, outbound_path__endswith=local_path.name
                    ).update(outbound_path=target)
                else:
                    mir_file = MIRFile.objects.filter(client=client, mir_filename=local_path.name).first()
                    if mir_file is not None:
                        set_mir_push_status(mir_file, True)
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
            "message": f"Sent {len(sent)} {label} outbound file(s) one by one."}


def execute_directional_operation(client, actor, automation_type, direction):
    key = (automation_type.upper(), direction.upper())

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
