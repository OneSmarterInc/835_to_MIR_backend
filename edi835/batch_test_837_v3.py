"""Conversion Test wrapper using the administrator's exact SFTP routes."""

import io
import json
import os
import posixpath
import stat
import uuid

from django.http import JsonResponse

from .admin_sftp_routes import resolve_admin_sftp_route
from .batch_test_837_v2 import _selected_client
from .edi837_naming_views import get_saved_837_filename_format, resolve_837_filename_format
from .edi837_service import ingest_837
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .views import api_start_batch_conversion as _original_api_start_batch_conversion


def _relay_837_for_test(request, client):
    """Process each 837 from 837_IN, then rename and deliver it to 837_OUT.

    Processing is intentionally completed before the outbound upload.  A file
    is only removed from 837_IN after it has been parsed/indexed, uploaded to
    837_OUT, and the outbound object has been verified with stat().
    """
    if client is None:
        return {
            "success": True,
            "transferred_count": 0,
            "processed_count": 0,
            "transferred": [],
            "message": "No client-scoped 837 relay was requested.",
        }

    try:
        _in_config, inbound_credentials, inbound_folder = resolve_admin_sftp_route(client, "837_IN")
        _out_config, outbound_credentials, outbound_folder = resolve_admin_sftp_route(client, "837_OUT")
    except Exception as exc:
        return {"success": False, "error": str(exc)}

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
        resolved_base = resolve_837_filename_format(filename_format)

        if not candidates:
            return {
                "success": True,
                "transferred_count": 0,
                "processed_count": 0,
                "transferred": [],
                "filename_format": filename_format,
                "resolved_filename": resolved_base,
                "inbound_folder": resolved_inbound,
                "outbound_folder": resolved_outbound,
                "message": f"No inbound 837 files were found in the admin-configured folder {resolved_inbound}.",
            }

        outbound_names = {
            entry.filename
            for entry in outbound_sftp.listdir_attr(resolved_outbound)
            if not stat.S_ISDIR(entry.st_mode)
        }

        stem, extension = os.path.splitext(resolved_base)
        extension = extension or ".837"
        multiple = len(candidates) > 1
        plan = []
        for index, source_name in enumerate(candidates, start=1):
            target_name = f"{stem}_{index:03d}{extension}" if multiple else resolved_base
            if target_name in outbound_names:
                return {
                    "success": False,
                    "error": f"Cannot relay 837 because {target_name} already exists in the admin-configured 837 outbound folder {resolved_outbound}.",
                }
            plan.append((source_name, target_name))

        transferred = []
        for source_name, target_name in plan:
            source_path = posixpath.join(resolved_inbound, source_name)
            target_path = posixpath.join(resolved_outbound, target_name)
            temp_path = posixpath.join(
                resolved_outbound,
                f".{target_name}.{uuid.uuid4().hex}.uploading",
            )

            with inbound_sftp.open(source_path, "rb") as source_file:
                payload = source_file.read()
            if not payload:
                raise ValueError(f"{source_name} is empty.")

            # Parse and index before anything is sent to 837_OUT.  ingest_837
            # also marks duplicate records as SFTP when the same bytes had
            # previously been indexed through a manual workflow.
            edi_file, duplicate = ingest_837(
                client,
                request.user,
                source_name,
                payload,
                import_mode="SFTP",
                remote_path=source_path,
            )
            if edi_file.status != "PROCESSED":
                raise RuntimeError(
                    f"{source_name} was not fully processed; current database status is {edi_file.status}."
                )
            if int(edi_file.claim_count or 0) <= 0:
                raise RuntimeError(f"{source_name} was not pushed because no 837 claims were indexed.")

            outbound_sftp.putfo(
                io.BytesIO(payload),
                temp_path,
                file_size=len(payload),
                confirm=True,
            )
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
                raise RuntimeError(
                    f"{source_name} reached 837 outbound but could not be removed from inbound; outbound was rolled back: {exc}"
                )

            # Persist the exact SFTP lifecycle used by the Search tables.
            edi_file.import_mode = "SFTP"
            edi_file.remote_path = source_path
            edi_file.outbound_path = target_path
            edi_file.save(update_fields=["import_mode", "remote_path", "outbound_path"])

            transferred.append({
                "file_id": str(edi_file.id),
                "from": source_name,
                "to": target_name,
                "inbound_path": source_path,
                "outbound_path": target_path,
                "status": edi_file.status,
                "inbound_source": "SFTP",
                "claim_count": edi_file.claim_count,
                "service_count": edi_file.service_count,
                "already_indexed": bool(duplicate),
            })

        return {
            "success": True,
            "transferred_count": len(transferred),
            "processed_count": len(transferred),
            "transferred": transferred,
            "filename_format": filename_format,
            "resolved_filename": resolved_base,
            "inbound_folder": resolved_inbound,
            "outbound_folder": resolved_outbound,
            "message": (
                f"Processed, indexed, renamed and moved {len(transferred)} 837 file(s) "
                "using the saved client naming format and admin-configured 837_IN/837_OUT routes."
            ),
        }
    except Exception as exc:
        return {"success": False, "error": f"837 Test relay failed: {exc}"}
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
        if client and str(client.stage or "").lower() == "offboarded":
            return JsonResponse(
                {
                    "success": False,
                    "code": "CLIENT_OFFBOARDED",
                    "offboarded": True,
                    "error": "This client has been permanently offboarded. SFTP transfers are locked.",
                },
                status=409,
            )

        relay_result = _relay_837_for_test(request, client)
        if not relay_result.get("success"):
            return JsonResponse(
                {
                    "success": False,
                    "error": relay_result.get("error") or "837 inbound-to-outbound relay failed.",
                    "sftp_837_transfer": relay_result,
                },
                status=400,
            )

        response = _original_api_start_batch_conversion(request)
        try:
            response_data = json.loads(response.content.decode("utf-8"))
            response_data["sftp_837_transfer"] = relay_result
            response.content = json.dumps(response_data).encode("utf-8")
            response["Content-Length"] = str(len(response.content))
        except Exception:
            pass
        return response

    return _original_api_start_batch_conversion(request)
