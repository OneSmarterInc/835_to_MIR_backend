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
    """Compatibility entry point; all relays use the guarded current pipeline."""
    from .batch_test_837_v3 import _relay_837_for_test as relay
    return relay(request, client)


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
