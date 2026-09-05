import io
import json
import os
import posixpath
import stat
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from project835.decorators import authenticated_api_required, json_api_errors
from project835.field_crypto import SFTPCredentialError, get_sftp_runtime_credentials

from .edi837_service import ingest_837
from .edi837_views import _client_for_request, _load_private_key, _safe_837_filename
from .file_types import has_valid_file_extension
from .services import resolve_sftp_config


def _open_sftp(paramiko, credentials):
    ssh = paramiko.SSHClient()
    if credentials.get("trust_unknown_key"):
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.load_system_host_keys()

    auth_method = credentials.get("auth_method")
    password = credentials.get("password")
    pkey = None
    if auth_method in ["SSH Key", "SSH Key + Password"]:
        pkey = _load_private_key(
            paramiko,
            credentials.get("ssh_key"),
            password=password,
        )
    pass_val = password if auth_method in ["Password", "SSH Key + Password"] else None

    ssh.connect(
        hostname=credentials.get("host"),
        port=int(credentials.get("port") or 22),
        username=credentials.get("username"),
        password=pass_val,
        pkey=pkey,
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
        look_for_keys=False,
        allow_agent=False,
    )
    return ssh, ssh.open_sftp()


def _normalize_folder(sftp, folder):
    try:
        return sftp.normalize(folder)
    except Exception:
        return folder


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_sftp_transfer(request):
    """Move valid 837 files from the configured 837 inbound route to 837 outbound.

    Files are copied to outbound under the administrator-selected filename first.
    The inbound source is removed only after the outbound upload is confirmed.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    # Staff may transfer for an explicitly authorized client. Standard client
    # users may transfer only for the client attached to their own account;
    # _client_for_request enforces that tenant boundary below.
    is_staff = bool(getattr(request.user, "is_staff", False))
    is_client_user = bool(getattr(request.user, "client_id", None)) and not is_staff
    if not is_staff and not is_client_user:
        return JsonResponse({"success": False, "error": "Client access is required."}, status=403)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    client = _client_for_request(request, body.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    if str(client.stage or "").lower() == "offboarded":
        return JsonResponse({"success": False, "error": "This client is offboarded; SFTP transfers are locked."}, status=409)

    filename = _safe_837_filename(body.get("filename"), fallback="")
    if not filename:
        return JsonResponse({"success": False, "error": "A valid 837 filename is required."}, status=400)
    stem, extension = os.path.splitext(filename)
    if not stem:
        return JsonResponse({"success": False, "error": "Enter a valid 837 filename."}, status=400)

    inbound_config = resolve_sftp_config(client=client, outbound=False, purpose="837_IN")
    if not inbound_config:
        return JsonResponse({"success": False, "error": "No 837 inbound SFTP configuration is available for this client."}, status=400)
    if inbound_config.status != "CONNECTED":
        return JsonResponse({"success": False, "error": "The selected client's 837 inbound SFTP connection is not connected."}, status=400)

    outbound_config = resolve_sftp_config(client=client, outbound=True, purpose="837_OUT")
    if not outbound_config:
        return JsonResponse({"success": False, "error": "No 837 outbound SFTP configuration is available for this client."}, status=400)
    if outbound_config.status != "CONNECTED":
        return JsonResponse({"success": False, "error": "The selected client's 837 outbound SFTP connection is not connected."}, status=400)

    try:
        inbound_credentials = get_sftp_runtime_credentials(inbound_config, outbound=False)
        outbound_credentials = get_sftp_runtime_credentials(outbound_config, outbound=True)
    except SFTPCredentialError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    inbound_folder = str(inbound_credentials.get("remote_folder") or "").strip()
    outbound_folder = str(outbound_credentials.get("remote_folder") or "").strip()
    if not inbound_folder:
        return JsonResponse({"success": False, "error": "The 837 inbound SFTP folder is not configured."}, status=400)
    if not outbound_folder:
        return JsonResponse({"success": False, "error": "The 837 outbound SFTP folder is not configured."}, status=400)

    import paramiko

    inbound_ssh = inbound_sftp = outbound_ssh = outbound_sftp = None
    uploaded_temp_paths = []
    try:
        inbound_ssh, inbound_sftp = _open_sftp(paramiko, inbound_credentials)
        outbound_ssh, outbound_sftp = _open_sftp(paramiko, outbound_credentials)

        resolved_inbound = _normalize_folder(inbound_sftp, inbound_folder)
        resolved_outbound = _normalize_folder(outbound_sftp, outbound_folder)

        inbound_entries = inbound_sftp.listdir_attr(resolved_inbound)
        candidates = sorted(
            entry.filename for entry in inbound_entries
            if not stat.S_ISDIR(entry.st_mode)
            and not entry.filename.startswith(".")
            and has_valid_file_extension(entry.filename, "837")
        )
        if not candidates:
            return JsonResponse({
                "success": True,
                "renamed_count": 0,
                "transferred_count": 0,
                "renamed": [],
                "transferred": [],
                "filename": filename,
                "message": "No valid 837 files were found in the inbound SFTP folder.",
            })

        outbound_entries = outbound_sftp.listdir_attr(resolved_outbound)
        outbound_names = {
            entry.filename for entry in outbound_entries
            if not stat.S_ISDIR(entry.st_mode)
        }

        multiple = len(candidates) > 1
        transfer_plan = []
        for index, source_name in enumerate(candidates, start=1):
            target_name = f"{stem}_{index:03d}{extension}" if multiple else filename
            if target_name in outbound_names:
                return JsonResponse({
                    "success": False,
                    "error": f"Cannot transfer because {target_name} already exists in the 837 outbound folder.",
                }, status=409)
            transfer_plan.append((source_name, target_name))

        transferred = []
        for source_name, target_name in transfer_plan:
            source_path = posixpath.join(resolved_inbound, source_name)
            target_path = posixpath.join(resolved_outbound, target_name)
            temporary_path = posixpath.join(
                resolved_outbound,
                f".{target_name}.{uuid.uuid4().hex}.uploading",
            )

            with inbound_sftp.open(source_path, "rb") as source_file:
                payload = source_file.read()
            if not payload:
                raise ValueError(f"{source_name} is empty.")

            # Keep the Search/claim index in sync with the exact file that is
            # being relayed through SFTP. Duplicate ingestion is safely ignored
            # by the existing 837 ingestion service.
            edi_file, duplicate = ingest_837(
                client,
                request.user,
                source_name,
                payload,
                import_mode="SFTP",
                remote_path=source_path,
                storage_filename=target_name,
            )

            outbound_sftp.putfo(
                io.BytesIO(payload),
                temporary_path,
                file_size=len(payload),
                confirm=True,
            )
            uploaded_temp_paths.append(temporary_path)
            outbound_sftp.rename(temporary_path, target_path)
            uploaded_temp_paths.remove(temporary_path)

            try:
                outbound_sftp.stat(target_path)
            except Exception as exc:
                raise RuntimeError(f"Outbound upload could not be verified for {target_name}: {exc}")

            # Only now is it safe to remove the inbound source. If cleanup
            # fails, roll back the outbound copy so a retry cannot duplicate it.
            try:
                inbound_sftp.remove(source_path)
            except Exception as exc:
                try:
                    outbound_sftp.remove(target_path)
                except Exception:
                    pass
                raise RuntimeError(
                    f"{source_name} reached outbound but could not be removed from inbound; "
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

        message = (
            f"Moved {len(transferred)} 837 file(s) from inbound to outbound SFTP using {filename}."
        )
        if multiple:
            message += " Numbered suffixes were added because more than one 837 file was present."

        return JsonResponse({
            "success": True,
            "renamed_count": len(transferred),
            "transferred_count": len(transferred),
            "renamed": transferred,
            "transferred": transferred,
            "filename": filename,
            "pushed_at": timezone.now().isoformat(),
            "message": message,
        })
    except Exception as exc:
        return JsonResponse({
            "success": False,
            "error": f"837 inbound-to-outbound transfer failed: {exc}",
        }, status=400)
    finally:
        if outbound_sftp:
            for temporary_path in uploaded_temp_paths:
                try:
                    outbound_sftp.remove(temporary_path)
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
