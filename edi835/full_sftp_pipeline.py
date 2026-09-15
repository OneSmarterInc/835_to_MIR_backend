"""End-to-end SFTP Test pipeline for 837, RECON and 835/MIR.

837 and RECON are processed sequentially one file at a time. Inbound 835 files
are collected as one conversion batch so every valid 835 present in the SFTP
folder contributes to one combined MIR. Source 835 files are removed from SFTP
only after that single MIR has been confirmed delivered. Failed/refused inputs
remain available for retry.
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
from .services import process_multiple_edi835_files
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
    """Combine every valid inbound 835 into ONE MIR, push it, then delete inputs.

    The SFTP folder is treated as one conversion batch regardless of whether it
    contains 2, 50, or more files. Files are read sequentially, then passed to
    the existing multi-835 converter, which validates each file independently
    and produces one MIR from all accepted claims. Refused/unreadable files are
    retained on inbound SFTP. Accepted files are deleted only after the single
    combined MIR has been confirmed uploaded to MIR_OUT.
    """
    _config, credentials, _folder = resolve_admin_sftp_route(client, "835_IN")
    import paramiko

    ssh = sftp = None
    candidate_names = []
    batch_items = []
    retained = []
    errors = []
    deleted_inputs = []

    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        folder = _normalize_folder(sftp, credentials["remote_folder"])
        entries = sorted(sftp.listdir_attr(folder), key=lambda item: item.filename)

        # Read remote objects one at a time. We keep only the text needed by the
        # combined converter; there is no per-file conversion or per-file MIR.
        for entry in entries:
            name = entry.filename
            if stat.S_ISDIR(entry.st_mode) or name.startswith(".") or not has_valid_file_extension(name, "835"):
                continue
            candidate_names.append(name)
            remote_path = posixpath.join(folder, name)
            try:
                with sftp.open(remote_path, "rb") as handle:
                    raw = handle.read()
                if not raw:
                    raise ValueError("835 file is empty")
                batch_items.append({
                    "filename": name,
                    "content": raw.decode("utf-8-sig", errors="replace").strip(),
                    "remote_path": remote_path,
                })
            except Exception as exc:
                errors.append(f"{name}: could not read inbound 835: {exc}")
                retained.append(name)

        if not candidate_names:
            return {
                "success": True,
                "processed_count": 0,
                "deleted_input_count": 0,
                "processed_files": [],
                "deleted_inputs": [],
                "retained_files": [],
                "sent_files": [],
                "errors": [],
                "combined_mir": "",
                "message": "No inbound 835 files were found.",
            }

        if not batch_items:
            return {
                "success": False,
                "processed_count": 0,
                "deleted_input_count": 0,
                "processed_files": [],
                "deleted_inputs": [],
                "retained_files": sorted(set(retained or candidate_names)),
                "sent_files": [],
                "errors": errors or ["No readable 835 files were available for combined conversion."],
                "combined_mir": "",
                "message": "No readable inbound 835 files could be converted.",
            }

        # This is the key contract: one invocation creates one MIR from every
        # valid 835 in the current SFTP folder.
        result = process_multiple_edi835_files(
            batch_items,
            ingestion_source="SFTP",
            client=client,
            deliver_outbound=True,
        )

        accepted = list(result.get("accepted_files") or [])
        refused = list(result.get("refused_files") or [])
        refused_names = {
            os.path.basename(str(item.get("filename") or ""))
            for item in refused
            if item.get("filename")
        }
        for item in refused:
            name = os.path.basename(str(item.get("filename") or ""))
            reasons = item.get("errors") or []
            detail = "; ".join(str(reason) for reason in reasons) or "835 validation refused the file"
            if name:
                errors.append(f"{name}: {detail}")
                retained.append(name)

        if not result.get("success"):
            # No successful combined MIR means no accepted source is safe to
            # delete. Keep every remote source available for correction/retry.
            retained.extend(name for name in candidate_names if name not in retained)
            return {
                "success": False,
                "processed_count": 0,
                "deleted_input_count": 0,
                "processed_files": accepted,
                "deleted_inputs": [],
                "retained_files": sorted(set(retained)),
                "sent_files": [],
                "errors": errors + [str(result.get("error") or "Combined MIR conversion failed")],
                "combined_mir": "",
                "message": "Combined 835 conversion failed; no inbound 835 files were deleted.",
            }

        combined_mir = str(result.get("combined_filename") or "")
        sftp_uploaded = bool(result.get("sftp_uploaded"))
        if not sftp_uploaded:
            delivery_error = str(result.get("sftp_error") or "Combined MIR was not confirmed on outbound SFTP")
            errors.append(delivery_error)
            # Conversion succeeded locally, but deletion is forbidden until the
            # single MIR is durably present on MIR_OUT.
            retained.extend(name for name in accepted if name not in retained)
            retained.extend(name for name in refused_names if name not in retained)
            return {
                "success": False,
                "processed_count": len(accepted),
                "deleted_input_count": 0,
                "processed_files": accepted,
                "deleted_inputs": [],
                "retained_files": sorted(set(retained)),
                "sent_files": [],
                "errors": errors,
                "combined_mir": combined_mir,
                "message": (
                    f"Combined {len(accepted)} accepted 835 file(s) into one MIR, but MIR delivery failed; "
                    "all inbound 835 files were retained."
                ),
            }

        # The ONE combined MIR is safely remote. Delete only source files that
        # actually contributed to it. Refused/unreadable files stay in 835_IN.
        accepted_set = set(accepted)
        for item in batch_items:
            name = item["filename"]
            if name not in accepted_set:
                continue
            remote_path = item["remote_path"]
            deleted, delete_error = _remove_with_retry(sftp, remote_path)
            if deleted:
                deleted_inputs.append(name)
            else:
                errors.append(f"{name}: combined MIR pushed but inbound delete failed: {delete_error}")
                retained.append(name)

        return {
            "success": not any(name in accepted_set for name in retained),
            "partial": bool(errors or refused_names),
            "processed_count": len(accepted),
            "deleted_input_count": len(deleted_inputs),
            "processed_files": accepted,
            "deleted_inputs": deleted_inputs,
            "retained_files": sorted(set(retained)),
            "sent_files": [combined_mir] if combined_mir else [],
            "errors": errors,
            "combined_mir": combined_mir,
            "accepted_file_count": len(accepted),
            "refused_file_count": len(refused_names),
            "message": (
                f"Combined {len(accepted)} accepted 835 file(s) into one MIR ({combined_mir or 'generated MIR'}); "
                f"deleted {len(deleted_inputs)} contributing inbound file(s) after confirmed MIR delivery."
            ),
        }
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


def run_full_sftp_pipeline(client, actor):
    """Run 837, RECON and combined 835/MIR end-to-end without a hard file-count cap."""
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
            "Full SFTP Test completed. Successful 837 files were pushed then removed from 837_IN; "
            "successful RECON files were processed then removed from RECON_IN; all valid 835 files present in "
            "835_IN were combined into one MIR, that single MIR was pushed to MIR_OUT, and only then were the "
            "contributing 835 source files removed from 835_IN."
        ),
    }
