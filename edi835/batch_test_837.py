import io
import json
import os
import posixpath
import stat
import uuid

from django.http import JsonResponse
from django.utils import timezone

from accounts.models import Client
from project835.field_crypto import SFTPCredentialError, get_sftp_runtime_credentials

from .edi837_service import ingest_837
from .edi837_transfer import _normalize_folder, _open_sftp
from .file_types import has_valid_file_extension
from .services import resolve_sftp_config
from .views import api_start_batch_conversion as _original_api_start_batch_conversion


def _selected_client(request, body):
    actor_client = getattr(request.user, "client", None)
    if actor_client is not None:
        return actor_client

    if not getattr(request.user, "is_staff", False):
        return None

    supplied_id = body.get("client_id") or body.get("client")
    if supplied_id is None or str(supplied_id).strip().lower() in {"", "none", "null", "undefined"}:
        return None

    try:
        client = Client.objects.get(id=supplied_id)
    except (Client.DoesNotExist, ValueError, TypeError):
        return None

    if getattr(request.user, "is_superuser", False):
        return client

    try:
        from admin_panel.access_control import has_active_client_grant
        return client if has_active_client_grant(request.user, client.id) else None
    except Exception:
        return None


def _default_837_filename():
    now = timezone.localtime()
    return now.strftime("%Y_%m_%d.837")


def _relay_837_for_test(request, client):
    """Relay inbound 837 files to 837 outbound before the normal Test batch.

    The exact source bytes are indexed, uploaded under the date-based 837 name,
    verified remotely, and only then removed from inbound. Multiple files get
    _001, _002, ... suffixes.
    """
    if client is None:
        return {"success": True, "transferred_count": 0, "transferred": [], "message": "No client-scoped 837 relay was requested."}

    inbound_config = resolve_sftp_config(client=client, outbound=False, purpose="837_IN")
    if not inbound_config:
        return {"success": True, "transferred_count": 0, "transferred": [], "message": "No 837 inbound SFTP configuration is available."}
    if inbound_config.status != "CONNECTED":
        return {"success": False, "error": f"837 inbound SFTP is not connected (status: {inbound_config.status})."}

    try:
        inbound_credentials = get_sftp_runtime_credentials(inbound_config, outbound=False)
    except SFTPCredentialError as exc:
        return {"success": False, "error": str(exc)}

    inbound_folder = str(inbound_credentials.get("remote_folder") or "").strip()
    if not inbound_folder:
        return {"success": False, "error": "The 837 inbound SFTP folder is not configured."}

    # First inspect inbound. If there are no 837s, do not require an outbound
    # route merely to let the rest of the existing Test workflow continue.
    import paramiko

    inbound_ssh = inbound_sftp = None
    outbound_ssh = outbound_sftp = None
    uploaded_temp_paths = []
    try:
        inbound_ssh, inbound_sftp = _open_sftp(paramiko, inbound_credentials)
        resolved_inbound = _normalize_folder(inbound_sftp, inbound_folder)
        entries = inbound_sftp.listdir_attr(resolved_inbound)
        candidates = sorted(
            entry.filename for entry in entries
            if not stat.S_ISDIR(entry.st_mode)
            and not entry.filename.startswith(".")
            and has_valid_file_extension(entry.filename, "837")
        )
        if not candidates:
            return {"success": True, "transferred_count": 0, "transferred": [], "message": "No inbound 837 files were found."}

        outbound_config = resolve_sftp_config(client=client, outbound=True, purpose="837_OUT")
        if not outbound_config:
            return {"success": False, "error": "Inbound 837 files were found, but no 837 outbound SFTP configuration is available for this client."}
        if outbound_config.status != "CONNECTED":
            return {"success": False, "error": f"837 outbound SFTP is not connected (status: {outbound_config.status})."}

        try:
            outbound_credentials = get_sftp_runtime_credentials(outbound_config, outbound=True)
        except SFTPCredentialError as exc:
            return {"success": False, "error": str(exc)}

        outbound_folder = str(outbound_credentials.get("remote_folder") or "").strip()
        if not outbound_folder:
            return {"success": False, "error": "The 837 outbound SFTP folder is not configured."}

        outbound_ssh, outbound_sftp = _open_sftp(paramiko, outbound_credentials)
        resolved_outbound = _normalize_folder(outbound_sftp, outbound_folder)
        outbound_entries = outbound_sftp.listdir_attr(resolved_outbound)
        outbound_names = {
            entry.filename for entry in outbound_entries
            if not stat.S_ISDIR(entry.st_mode)
        }

        base_filename = _default_837_filename()
        stem, extension = os.path.splitext(base_filename)
        multiple = len(candidates) > 1
        plan = []
        for index, source_name in enumerate(candidates, start=1):
            target_name = f"{stem}_{index:03d}{extension}" if multiple else base_filename
            if target_name in outbound_names:
                return {"success": False, "error": f"Cannot relay 837 because {target_name} already exists in the outbound folder."}
            plan.append((source_name, target_name))

        transferred = []
        for source_name, target_name in plan:
            source_path = posixpath.join(resolved_inbound, source_name)
            target_path = posixpath.join(resolved_outbound, target_name)
            temp_path = posixpath.join(resolved_outbound, f".{target_name}.{uuid.uuid4().hex}.uploading")

            with inbound_sftp.open(source_path, "rb") as source_file:
                payload = source_file.read()
            if not payload:
                raise ValueError(f"{source_name} is empty.")

            edi_file, duplicate = ingest_837(
                client,
                request.user,
                source_name,
                payload,
                import_mode="SFTP",
            )

            outbound_sftp.putfo(io.BytesIO(payload), temp_path, file_size=len(payload), confirm=True)
            uploaded_temp_paths.append(temp_path)
            outbound_sftp.rename(temp_path, target_path)
            uploaded_temp_paths.remove(temp_path)
            outbound_sftp.stat(target_path)

            try:
                inbound_sftp.remove(source_path)
            except Exception as exc:
                try:
                    outbound_sftp.remove(target_path)
                except Exception:
                    pass
                raise RuntimeError(
                    f"{source_name} reached 837 outbound but could not be removed from inbound; "
                    f"the outbound copy was rolled back: {exc}"
                )

            if hasattr(edi_file, "outbound_path"):
                edi_file.outbound_path = target_path
                edi_file.save(update_fields=["outbound_path"])

            transferred.append({
                "from": source_name,
                "to": target_name,
                "inbound_path": source_path,
                "outbound_path": target_path,
                "already_indexed": bool(duplicate),
            })

        return {
            "success": True,
            "transferred_count": len(transferred),
            "transferred": transferred,
            "filename": base_filename,
            "message": f"Moved {len(transferred)} 837 file(s) from inbound to outbound before running the normal Test batch.",
        }
    except Exception as exc:
        return {"success": False, "error": f"837 Test relay failed: {exc}"}
    finally:
        if outbound_sftp:
            for temp_path in uploaded_temp_paths:
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
    """Preserve the existing Test endpoint and prepend the 837 relay step."""
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else {}
        except (TypeError, ValueError, UnicodeDecodeError):
            body = {}

        client = _selected_client(request, body)
        requested_client = body.get("client_id") or body.get("client")
        if requested_client and client is None:
            return JsonResponse({"success": False, "error": "The selected client was not found or is not authorized."}, status=403)

        if client and str(client.stage or "").lower() == "offboarded":
            return JsonResponse({
                "success": False,
                "code": "CLIENT_OFFBOARDED",
                "offboarded": True,
                "error": "This client has been permanently offboarded. SFTP transfers are locked.",
            }, status=409)

        relay_result = _relay_837_for_test(request, client)
        if not relay_result.get("success"):
            return JsonResponse({
                "success": False,
                "error": relay_result.get("error") or "837 inbound-to-outbound relay failed.",
                "sftp_837_transfer": relay_result,
            }, status=400)

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
