"""Route-aware SFTP browser.

The old browser chose inbound vs outbound credentials from connection_type.
That is wrong for DEFAULT/UNIFIED configurations: browsing an admin-configured
837_OUT or MIR_OUT path could still connect to the inbound side.  This wrapper
infers the transfer purpose from the saved admin paths and uses the same
authoritative resolver as the transfer pipeline.
"""

import json
import posixpath
import stat
from datetime import datetime, timezone as dt_timezone

from django.http import JsonResponse

from .admin_sftp_routes import VALID_PURPOSES, resolve_admin_sftp_route
from .edi837_transfer import _normalize_folder, _open_sftp
from .models import SFTPConfig
from .views import api_browse_sftp as _legacy_api_browse_sftp


def _clean_path(value):
    value = str(value or "").strip()
    if not value:
        return "."
    if value != "/":
        value = value.rstrip("/")
    return value or "/"


def _path_is_under(path, root):
    path = _clean_path(path)
    root = _clean_path(root)
    if root in {"", "."}:
        return False
    if path == root:
        return True
    prefix = root.rstrip("/") + "/"
    return path.startswith(prefix)


def _infer_purpose(config, requested_path):
    requested_path = _clean_path(requested_path)

    explicit = str(getattr(config, "purpose", "") or "").upper()
    if explicit in VALID_PURPOSES:
        return explicit

    candidates = []
    rows = SFTPConfig.objects.filter(client_id=config.client_id) if config.client_id else SFTPConfig.objects.filter(client__isnull=True)
    for row in rows.order_by("-updated_at"):
        row_purpose = str(row.purpose or "").upper()
        if row_purpose in VALID_PURPOSES and row.remote_folder:
            candidates.append((row_purpose, _clean_path(row.remote_folder)))
        for purpose, folder in (row.route_paths or {}).items():
            purpose = str(purpose or "").upper()
            if purpose in VALID_PURPOSES and folder:
                candidates.append((purpose, _clean_path(folder)))

    # Prefer the longest matching route so nested admin paths resolve correctly.
    matches = [(purpose, folder) for purpose, folder in candidates if _path_is_under(requested_path, folder)]
    if matches:
        matches.sort(key=lambda item: len(item[1]), reverse=True)
        return matches[0][0]
    return None


def _parent_path(path):
    path = _clean_path(path)
    if path in {"/", "."}:
        return None
    parent = posixpath.dirname(path.rstrip("/")) or "/"
    return parent if parent != path else None


def api_browse_sftp_admin_routes(request):
    if request.method != "POST":
        return _legacy_api_browse_sftp(request)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        body = {}

    config_id = str(body.get("config_id") or body.get("id") or "").strip()
    requested_path = _clean_path(body.get("path") or ".")
    requested_purpose = str(body.get("purpose") or "").strip().upper()

    if not config_id:
        return _legacy_api_browse_sftp(request)

    config_qs = SFTPConfig.objects.filter(id=config_id)
    actor_client_id = getattr(request.user, "client_id", None)
    if actor_client_id:
        config_qs = config_qs.filter(client_id=actor_client_id)
    elif not getattr(request.user, "is_staff", False):
        config_qs = config_qs.none()

    config = config_qs.first()
    if not config:
        return JsonResponse({"success": False, "error": "SFTP configuration was not found or is not authorized."}, status=404)

    purpose = requested_purpose if requested_purpose in VALID_PURPOSES else _infer_purpose(config, requested_path)
    if not purpose:
        # Non-route browsing retains the legacy behavior.
        return _legacy_api_browse_sftp(request)

    client = config.client
    try:
        _route_config, credentials, admin_folder = resolve_admin_sftp_route(client, purpose)
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc), "purpose": purpose}, status=400)

    # If the browser was opened for this route, keep navigation inside the
    # actual admin-selected server/folder.  The supplied path may be a child.
    browse_path = requested_path
    if browse_path in {"", "."}:
        browse_path = admin_folder

    import paramiko
    ssh = sftp = None
    try:
        ssh, sftp = _open_sftp(paramiko, credentials)
        resolved = _normalize_folder(sftp, browse_path)
        attrs = sftp.listdir_attr(resolved)

        folders = []
        files = []
        for attr in sorted(attrs, key=lambda item: item.filename.lower()):
            child_path = posixpath.join(resolved.rstrip("/"), attr.filename) if resolved != "/" else f"/{attr.filename}"
            try:
                mtime = datetime.fromtimestamp(attr.st_mtime, tz=dt_timezone.utc).isoformat() if attr.st_mtime else None
            except Exception:
                mtime = None
            if stat.S_ISDIR(attr.st_mode):
                folders.append({"name": attr.filename, "path": child_path, "mtime": mtime})
            else:
                files.append({"name": attr.filename, "path": child_path, "size": int(attr.st_size or 0), "mtime": mtime})

        return JsonResponse({
            "success": True,
            "purpose": purpose,
            "configured_route": admin_folder,
            "pwd": resolved,
            "parent_path": _parent_path(resolved),
            "folders": folders,
            "files": files,
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": f"Could not browse {purpose} SFTP route: {exc}", "purpose": purpose}, status=400)
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
