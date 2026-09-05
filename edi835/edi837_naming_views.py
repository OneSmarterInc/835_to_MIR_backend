"""Token-based 837 filename handling for Search rename and sliced claim SFTP push."""

import io
import json
import os
import posixpath
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from project835.decorators import authenticated_api_required, json_api_errors

from .admin_sftp_routes import resolve_admin_sftp_route
from .edi837_service import export_single_claim
from .edi837_transfer import _normalize_folder, _open_sftp, edi837_sftp_transfer as _legacy_sftp_transfer
from .edi837_views import _client_for_request, _safe_837_filename, _visible_claim
from .models import SFTPConfig


DEFAULT_837_FILENAME_FORMAT = "YYYYMMDDhhmmss.837"
FORMAT_ROUTE_KEY = "837_FILENAME_FORMAT"


def get_saved_837_filename_format(client):
    """Return the server-side filename template saved for this client."""
    if client is None:
        return DEFAULT_837_FILENAME_FORMAT
    rows = SFTPConfig.objects.filter(client=client).order_by("-updated_at")
    preferred = rows.filter(purpose="DEFAULT").first()
    if preferred:
        value = str((preferred.route_paths or {}).get(FORMAT_ROUTE_KEY) or "").strip()
        if value:
            return value
    for purpose in ("837_IN", "837_OUT"):
        row = rows.filter(purpose=purpose).first()
        if row:
            value = str((row.route_paths or {}).get(FORMAT_ROUTE_KEY) or "").strip()
            if value:
                return value
    for row in rows:
        value = str((row.route_paths or {}).get(FORMAT_ROUTE_KEY) or "").strip()
        if value:
            return value
    return DEFAULT_837_FILENAME_FORMAT


def save_837_filename_format(client, value):
    """Persist one literal tokenized filename template per client.

    Reuse the client's SFTP config JSON so no unrelated schema migration is
    required.  The special metadata key is ignored by route resolution.
    """
    if client is None:
        raise ValueError("Select a client before saving the 837 filename format.")
    template = _safe_837_filename(value or DEFAULT_837_FILENAME_FORMAT, fallback=DEFAULT_837_FILENAME_FORMAT)
    rows = SFTPConfig.objects.filter(client=client).order_by("-updated_at")
    row = rows.filter(purpose="DEFAULT").first()
    if row is None:
        row = rows.filter(purpose="837_IN").first() or rows.filter(purpose="837_OUT").first() or rows.first()
    if row is None:
        raise ValueError("Save the client's SFTP configuration before saving the 837 filename format.")
    paths = dict(row.route_paths or {})
    paths[FORMAT_ROUTE_KEY] = template
    row.route_paths = paths
    row.save(update_fields=["route_paths", "updated_at"])
    return template


def resolve_837_filename_format(value, now=None):
    """Expand YYYYMMDDhhmmss tokens while preserving user-supplied static text."""
    template = str(value or DEFAULT_837_FILENAME_FORMAT).strip() or DEFAULT_837_FILENAME_FORMAT
    now = timezone.localtime(now or timezone.now())
    replacements = (
        ("YYYY", now.strftime("%Y")),
        ("MM", now.strftime("%m")),
        ("DD", now.strftime("%d")),
        ("hh", now.strftime("%H")),
        ("mm", now.strftime("%M")),
        ("ss", now.strftime("%S")),
    )
    resolved = template
    for token, replacement in replacements:
        resolved = resolved.replace(token, replacement)
    return _safe_837_filename(resolved, fallback=now.strftime("%Y%m%d%H%M%S.837"))


def sliced_claim_filename(template, claim_number, now=None):
    """Resolve the base format and append the sliced claim number before .837."""
    base = resolve_837_filename_format(template, now=now)
    stem, extension = os.path.splitext(base)
    safe_claim = "".join(
        char for char in str(claim_number or "") if char.isalnum() or char in "-_"
    )
    safe_claim = safe_claim or "claim"
    return _safe_837_filename(f"{stem}_{safe_claim}{extension or '.837'}")


def _with_body(request, payload, callback, *args, **kwargs):
    previous = getattr(request, "_body", None)
    had_body = hasattr(request, "_body")
    request._body = json.dumps(payload).encode("utf-8")
    try:
        return callback(request, *args, **kwargs)
    finally:
        if had_body:
            request._body = previous
        else:
            try:
                delattr(request, "_body")
            except AttributeError:
                pass


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_sftp_transfer_named(request):
    """Save the client's template and use it for Search Rename."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    client = _client_for_request(request, body.get("client_id") or body.get("client"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)

    template = body.get("filename_format") or body.get("filename") or get_saved_837_filename_format(client)
    try:
        template = save_837_filename_format(client, template)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    resolved = resolve_837_filename_format(template)
    forwarded = dict(body)
    forwarded["filename"] = resolved
    response = _with_body(request, forwarded, _legacy_sftp_transfer)
    try:
        data = json.loads(response.content.decode("utf-8"))
        data["filename_format"] = template
        data["resolved_filename"] = resolved
        data["format_saved"] = True
        response.content = json.dumps(data).encode("utf-8")
        response["Content-Length"] = str(len(response.content))
    except Exception:
        pass
    return response


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_claim_push_sftp_named(request, claim_id):
    """Push one sliced claim using saved format + _<claim number>.837."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)

    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    if str(claim.client.stage or "").lower() == "offboarded":
        return JsonResponse({"success": False, "error": "This client is offboarded; SFTP transfers are locked."}, status=409)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    template = body.get("filename_format") or body.get("filename") or get_saved_837_filename_format(claim.client)
    filename = sliced_claim_filename(template, claim.claim_control_number)

    try:
        _config, credentials, outbound_folder = resolve_admin_sftp_route(claim.client, "837_OUT")
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    import paramiko

    ssh = sftp = None
    temporary_path = ""
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        resolved_folder = _normalize_folder(sftp, outbound_folder)
        target_path = posixpath.join(resolved_folder, filename)

        try:
            sftp.stat(target_path)
        except (FileNotFoundError, OSError) as exc:
            if not isinstance(exc, FileNotFoundError) and getattr(exc, "errno", None) not in {2, None}:
                raise
        else:
            return JsonResponse({"success": False, "error": f"{filename} already exists in the 837 outbound folder."}, status=409)

        temporary_path = posixpath.join(resolved_folder, f".{filename}.{uuid.uuid4().hex}.uploading")
        payload = export_single_claim(claim).encode("utf-8")
        sftp.putfo(io.BytesIO(payload), temporary_path, file_size=len(payload), confirm=True)
        sftp.rename(temporary_path, target_path)
        temporary_path = ""
        sftp.stat(target_path)

        return JsonResponse({
            "success": True,
            "filename_format": str(template),
            "filename": filename,
            "claim_number": claim.claim_control_number,
            "remote_path": target_path,
            "pushed_at": timezone.now().isoformat(),
            "message": f"Sliced claim {claim.claim_control_number} was pushed as {filename}.",
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": f"837 sliced claim SFTP push failed: {exc}"}, status=400)
    finally:
        if sftp:
            if temporary_path:
                try:
                    sftp.remove(temporary_path)
                except Exception:
                    pass
            try:
                sftp.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
