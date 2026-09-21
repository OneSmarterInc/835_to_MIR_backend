"""Conversion Test wrapper using the administrator's exact SFTP routes."""

import io
import json
import os
import posixpath
import stat
import uuid

from django.http import JsonResponse
from django.utils import timezone

from .admin_sftp_routes import resolve_admin_sftp_route
from .batch_jobs import active_job_for, write_job
from .batch_test_837_v2 import _selected_client
from .edi837_naming_views import get_saved_837_filename_format, resolve_837_filename_format
from .edi837_service import ingest_837
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .views import api_start_batch_conversion as _original_api_start_batch_conversion


def _append_hhss(filename, now=None):
    """Append the requested hour+seconds stamp immediately before the extension."""
    stamp = timezone.localtime(now or timezone.now()).strftime("%H%S")
    stem, extension = os.path.splitext(os.path.basename(filename))
    return f"{stem}_{stamp}{extension}"


def _available_name(base_name, existing):
    """Return a collision-free outbound name without stopping the whole batch."""
    if base_name not in existing:
        return base_name
    stem, extension = os.path.splitext(base_name)
    for suffix in range(1, 100000):
        candidate = f"{stem}_{suffix:03d}{extension}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"Could not allocate a unique outbound name for {base_name}.")


def _relay_837_for_test(request, client):
    """Process 837_IN -> index -> 837_OUT -> delete source, one file at a time."""
    if client is None:
        return {
            "success": True,
            "transferred_count": 0,
            "processed_count": 0,
            "transferred": [],
            "errors": [],
            "message": "No client-scoped 837 relay was requested.",
        }

    try:
        _in_config, inbound_credentials, inbound_folder = resolve_admin_sftp_route(client, "837_IN")
        _out_config, outbound_credentials, outbound_folder = resolve_admin_sftp_route(client, "837_OUT")
    except Exception as exc:
        return {"success": False, "error": str(exc), "errors": [str(exc)]}

    import paramiko

    inbound_ssh = inbound_sftp = outbound_ssh = outbound_sftp = None
    temp_paths = []
    try:
        inbound_ssh, inbound_sftp = _open_sftp(paramiko, inbound_credentials)
        outbound_ssh, outbound_sftp = _open_sftp(paramiko, outbound_credentials)
        resolved_inbound = _normalize_folder(inbound_sftp, inbound_folder)
        resolved_outbound = _normalize_folder(outbound_sftp, outbound_folder)

        entries = inbound_sftp.listdir_attr(resolved_inbound)
        candidates = sorted(
            entry.filename
            for entry in entries
            if not stat.S_ISDIR(entry.st_mode)
            and not entry.filename.startswith(".")
            and has_valid_file_extension(entry.filename, "837")
        )

        filename_format = get_saved_837_filename_format(client)
        resolved_base = _append_hhss(resolve_837_filename_format(filename_format))
        if not candidates:
            return {
                "success": True,
                "transferred_count": 0,
                "processed_count": 0,
                "transferred": [],
                "errors": [],
                "filename_format": filename_format,
                "resolved_filename": resolved_base,
                "inbound_folder": resolved_inbound,
                "outbound_folder": resolved_outbound,
                "message": f"No inbound 837 files were found in {resolved_inbound}.",
            }

        outbound_names = {
            entry.filename
            for entry in outbound_sftp.listdir_attr(resolved_outbound)
            if not stat.S_ISDIR(entry.st_mode)
        }
        stem, extension = os.path.splitext(resolved_base)
        extension = extension or ".837"

        transferred, errors = [], []
        total = len(candidates)
        for index, source_name in enumerate(candidates, start=1):
            requested = f"{stem}_{index:03d}{extension}" if total > 1 else resolved_base
            target_name = _available_name(requested, outbound_names)
            source_path = posixpath.join(resolved_inbound, source_name)
            target_path = posixpath.join(resolved_outbound, target_name)
            temp_path = posixpath.join(resolved_outbound, f".{target_name}.{uuid.uuid4().hex}.uploading")
            try:
                with inbound_sftp.open(source_path, "rb") as source_file:
                    payload = source_file.read()
                if not payload:
                    raise ValueError("file is empty")

                edi_file, duplicate = ingest_837(
                    client,
                    request.user,
                    source_name,
                    payload,
                    import_mode="SFTP",
                    remote_path=source_path,
                    storage_filename=target_name,
                )
                if edi_file.status != "PROCESSED":
                    raise RuntimeError(f"database status is {edi_file.status}, not PROCESSED")
                if int(edi_file.claim_count or 0) <= 0:
                    raise RuntimeError("no 837 claims were indexed")

                outbound_sftp.putfo(io.BytesIO(payload), temp_path, file_size=len(payload), confirm=True)
                temp_paths.append(temp_path)
                outbound_sftp.rename(temp_path, target_path)
                temp_paths.remove(temp_path)
                outbound_sftp.stat(target_path)

                try:
                    inbound_sftp.remove(source_path)
                except Exception as exc:
                    try:
                        outbound_sftp.remove(target_path)
                    except Exception:
                        pass
                    raise RuntimeError(f"outbound uploaded, but inbound delete failed; outbound rolled back: {exc}")

                edi_file.import_mode = "SFTP"
                edi_file.remote_path = source_path
                edi_file.outbound_path = target_path
                edi_file.save(update_fields=["import_mode", "remote_path", "outbound_path"])
                outbound_names.add(target_name)
                transferred.append({
                    "file_id": str(edi_file.id),
                    "from": source_name,
                    "to": target_name,
                    "inbound_path": source_path,
                    "outbound_path": target_path,
                    "status": edi_file.status,
                    "claim_count": edi_file.claim_count,
                    "service_count": edi_file.service_count,
                    "already_indexed": bool(duplicate),
                })
                del payload
            except Exception as exc:
                try:
                    outbound_sftp.remove(temp_path)
                except Exception:
                    pass
                errors.append(f"{source_name}: {exc}")
                # Continue with the next file; the failed inbound object remains
                # in 837_IN for a safe retry.
                continue

        return {
            "success": not errors or bool(transferred),
            "partial": bool(errors),
            "transferred_count": len(transferred),
            "processed_count": len(transferred),
            "transferred": transferred,
            "errors": errors,
            "filename_format": filename_format,
            "resolved_filename": resolved_base,
            "inbound_folder": resolved_inbound,
            "outbound_folder": resolved_outbound,
            "message": f"Processed and relayed {len(transferred)} of {len(candidates)} 837 file(s) one by one.",
        }
    except Exception as exc:
        return {"success": False, "error": f"837 Test relay failed: {exc}", "errors": [str(exc)]}
    finally:
        if outbound_sftp:
            for temp_path in temp_paths:
                try:
                    outbound_sftp.remove(temp_path)
                except Exception:
                    pass
            try:
                outbound_sftp.close()
            except Exception:
                pass
        if outbound_ssh:
            try:
                outbound_ssh.close()
            except Exception:
                pass
        if inbound_sftp:
            try:
                inbound_sftp.close()
            except Exception:
                pass
        if inbound_ssh:
            try:
                inbound_ssh.close()
            except Exception:
                pass


def api_start_batch_conversion_with_837(request):
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else {}
        except (TypeError, ValueError, UnicodeDecodeError):
            body = {}

        client = _selected_client(request, body)
        requested_client = body.get("client_id") or body.get("client")
        if requested_client and client is None:
            return JsonResponse(
                {"success": False, "error": "The selected client was not found or is not authorized."},
                status=403,
            )
        if client is None:
            return _original_api_start_batch_conversion(request)
        if str(client.stage or "").lower() == "offboarded":
            return JsonResponse(
                {
                    "success": False,
                    "code": "CLIENT_OFFBOARDED",
                    "offboarded": True,
                    "error": "This client has been permanently offboarded. SFTP transfers are locked.",
                },
                status=409,
            )

        # Test is an end-to-end SFTP pipeline, not merely a local parser run.
        # Heavy I/O remains in the isolated worker so 50+ files per folder do
        # not hold a Gunicorn request open.
        scope_key = f"{client.id}:ALL:FULL_PIPELINE"
        existing = active_job_for(scope_key)
        if existing:
            return JsonResponse({
                "success": False,
                "error": "A full SFTP Test is already queued or running for this client.",
                "job_id": existing["id"],
                "state": existing["state"],
            }, status=409)

        job_id = str(uuid.uuid4())
        write_job({
            "id": job_id,
            "owner_user_id": str(request.user.id),
            "client_id": str(client.id),
            "automation_type": "ALL",
            "automation_direction": "FULL_PIPELINE",
            "scope_key": scope_key,
            "state": "QUEUED",
            "started_at": timezone.now().isoformat(),
            "worker_started_at": None,
            "finished_at": None,
            "status_code": None,
            "result": None,
            "attempt_count": 0,
            "retry_count": 0,
        })
        return JsonResponse({
            "success": True,
            "queued": True,
            "job_id": job_id,
            "state": "QUEUED",
            "message": "Full SFTP Test queued. 837, RECON and 835/MIR will run sequentially in the isolated worker.",
        }, status=202)

    # Preserve the existing GET job-status endpoint used by the frontend poller.
    return _original_api_start_batch_conversion(request)
