"""End-to-end SFTP Test pipeline for 837, RECON and 835/MIR.

The pipeline is intentionally sequential. Each inbound object is processed one
at a time; inbound files are only removed after the durable downstream step has
succeeded. This keeps 50+ file folders safe from request timeouts and memory
spikes while preserving failed files for retry.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import uuid
from types import SimpleNamespace

from django.db import IntegrityError, transaction

from .admin_sftp_routes import resolve_admin_sftp_route
from .batch_test_837_v3 import _relay_837_for_test
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .models import EDI835File, RECONFile
from .recon_service import process_recon_file
from .services import process_edi835_file_content
from .sftp_automation_operations import push_local_outbound
from .storage import stage_inbound


def _remove_with_retry(sftp, remote_path, attempts=3):
    last_error = None
    for _ in range(max(1, attempts)):
        try:
            sftp.remove(remote_path)
            return True, ""
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return True, ""
            last_error = exc
        except Exception as exc:
            last_error = exc
    return False, str(last_error or "Unknown SFTP delete error")


def _decode_recon(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def process_recon_incoming(client, actor):
    """Process every RECON file one by one and delete only successful inputs."""
    _config, credentials, _folder = resolve_admin_sftp_route(client, "RECON_IN")
    import paramiko

    ssh = sftp = None
    processed, errors, retained = [], [], []
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        folder = _normalize_folder(sftp, credentials["remote_folder"])
        entries = sorted(sftp.listdir_attr(folder), key=lambda item: item.filename)
        for entry in entries:
            name = entry.filename
            if stat.S_ISDIR(entry.st_mode) or name.startswith(".") or not has_valid_file_extension(name, "RECON"):
                continue
            remote_path = posixpath.join(folder, name)
            try:
                with sftp.open(remote_path, "rb") as handle:
                    raw = handle.read()
                if not raw:
                    raise ValueError("RECON file is empty")
                text = _decode_recon(raw)
                if "\x00" in text:
                    raise ValueError("RECON file contains binary/NUL data")

                file_hash = hashlib.sha256(raw).hexdigest()
                existing = RECONFile.objects.filter(client=client, file_hash=file_hash).first()
                if existing is not None:
                    recon = existing
                else:
                    stored = f"{getattr(client, 'client_code', 'CLIENT')}_{uuid.uuid4()}_{os.path.basename(name)}"[:255]
                    try:
                        with transaction.atomic():
                            recon = RECONFile.objects.create(
                                client=client,
                                uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
                                original_filename=os.path.basename(name)[:255],
                                stored_filename=stored,
                                file_content=text,
                                file_hash=file_hash,
                                file_size=len(raw),
                                import_mode="SFTP",
                            )
                            stage_inbound(client, "recon", stored, raw, binary=True)
                    except IntegrityError:
                        recon = RECONFile.objects.get(client=client, file_hash=file_hash)

                if recon.status not in {"PROCESSED", "COMPLETED"}:
                    process_recon_file(recon, actor)
                    recon.refresh_from_db()

                if recon.status in {"PROCESSED", "COMPLETED"}:
                    deleted, delete_error = _remove_with_retry(sftp, remote_path)
                    if not deleted:
                        errors.append(f"{name}: processed but inbound delete failed: {delete_error}")
                        retained.append(name)
                        continue
                    processed.append(name)
                else:
                    errors.append(f"{name}: RECON processing ended with status {recon.status}")
                    retained.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                retained.append(name)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass

    return {
        "success": not errors or bool(processed),
        "processed_count": len(processed),
        "processed_files": processed,
        "retained_files": retained,
        "errors": errors,
        "message": f"Processed {len(processed)} RECON file(s) one by one; successful inputs were removed from SFTP.",
    }


def process_835_to_mir_sftp(client, actor):
    """Read each inbound 835, convert it, confirm MIR delivery, then delete that 835."""
    _config, credentials, _folder = resolve_admin_sftp_route(client, "835_IN")
    import paramiko

    ssh = sftp = None
    processed, deleted_inputs, retained, errors, mir_sent = [], [], [], [], []
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        folder = _normalize_folder(sftp, credentials["remote_folder"])
        entries = sorted(sftp.listdir_attr(folder), key=lambda item: item.filename)
        for entry in entries:
            name = entry.filename
            if stat.S_ISDIR(entry.st_mode) or name.startswith(".") or not has_valid_file_extension(name, "835"):
                continue
            remote_path = posixpath.join(folder, name)
            try:
                with sftp.open(remote_path, "rb") as handle:
                    raw = handle.read()
                if not raw:
                    raise ValueError("835 file is empty")
                text = raw.decode("utf-8-sig", errors="replace").strip()

                result = process_edi835_file_content(
                    text,
                    original_filename=name,
                    ingestion_source="SFTP",
                    client=client,
                )
                record = result.get("db_record")
                if not result.get("success") or record is None:
                    errors.append(f"{name}: {result.get('error') or '835 conversion failed'}")
                    retained.append(name)
                    continue

                processed.append(name)
                record.refresh_from_db()
                mir = getattr(record, "mir_file", None)
                if mir is None:
                    errors.append(f"{name}: conversion completed but no MIR record was created")
                    retained.append(name)
                    continue
                mir.refresh_from_db()

                # Normal conversion already attempts the configured MIR SFTP
                # delivery. Only drain the local MIR queue if that direct push
                # did not succeed, avoiding duplicate remote files.
                push_result = {"sent_files": [], "errors": []}
                if mir.status != "PUSHED":
                    push_result = push_local_outbound(client, "mir")
                    mir_sent.extend(push_result.get("sent_files") or [])
                    mir.refresh_from_db()

                if mir.status != "PUSHED":
                    detail = "; ".join(push_result.get("errors") or []) or "MIR was not confirmed on outbound SFTP"
                    errors.append(f"{name}: {detail}")
                    retained.append(name)
                    continue

                deleted, delete_error = _remove_with_retry(sftp, remote_path)
                if not deleted:
                    errors.append(f"{name}: MIR pushed but inbound delete failed: {delete_error}")
                    retained.append(name)
                    continue
                deleted_inputs.append(name)
                EDI835File.objects.filter(id=record.id).update(present_in_sftp=False)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                retained.append(name)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass

    return {
        "success": not errors or bool(deleted_inputs),
        "processed_count": len(processed),
        "deleted_input_count": len(deleted_inputs),
        "processed_files": processed,
        "deleted_inputs": deleted_inputs,
        "retained_files": retained,
        "sent_files": mir_sent,
        "errors": errors,
        "message": (
            f"Converted {len(processed)} 835 file(s); confirmed and deleted {len(deleted_inputs)} inbound file(s) "
            "only after MIR delivery."
        ),
    }


def run_full_sftp_pipeline(client, actor):
    """Run 837, RECON and 835/MIR end-to-end without a hard file-count cap."""
    request = SimpleNamespace(user=actor)
    stages = {}
    stage_errors = []

    try:
        stages["837"] = _relay_837_for_test(request, client)
    except Exception as exc:
        stages["837"] = {"success": False, "error": str(exc)}
    if not stages["837"].get("success"):
        stage_errors.append(stages["837"].get("error") or "837 stage failed")

    try:
        stages["recon"] = process_recon_incoming(client, actor)
    except Exception as exc:
        stages["recon"] = {"success": False, "error": str(exc)}
    if not stages["recon"].get("success"):
        stage_errors.append(stages["recon"].get("error") or "RECON stage failed")

    try:
        stages["835_mir"] = process_835_to_mir_sftp(client, actor)
    except Exception as exc:
        stages["835_mir"] = {"success": False, "error": str(exc)}
    if not stages["835_mir"].get("success"):
        stage_errors.append(stages["835_mir"].get("error") or "835/MIR stage failed")

    file_errors = []
    for stage in stages.values():
        file_errors.extend(stage.get("errors") or [])

    return {
        "success": not stage_errors,
        "partial": bool(stage_errors or file_errors),
        "stages": stages,
        "errors": stage_errors + file_errors,
        "processed_count": sum(int(stage.get("processed_count") or stage.get("transferred_count") or 0) for stage in stages.values()),
        "message": (
            "Full SFTP Test completed sequentially. Successful 837 files were pushed then removed from 837_IN; "
            "successful RECON files were processed then removed from RECON_IN; successful 835 files were converted, "
            "their MIR outputs were pushed, and only then were the source 835 files removed from 835_IN."
        ),
    }
