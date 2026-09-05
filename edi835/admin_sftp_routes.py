"""Authoritative admin-configured SFTP route resolver.

Every transfer purpose must use the folder saved by the administrator for that
purpose.  Generic/legacy 835 or MIR folders are never substituted for another
route.
"""

from project835.field_crypto import get_sftp_runtime_credentials

from .models import SFTPConfig

VALID_PURPOSES = {"837_IN", "837_OUT", "835_IN", "MIR_OUT", "RECON_IN"}


def _clean(value):
    return str(value or "").strip()


def _folder_from_row(row, purpose):
    if row is None:
        return ""

    # Dedicated purpose row: its own remote_folder is authoritative.
    if _clean(getattr(row, "purpose", "")).upper() == purpose:
        folder = _clean(getattr(row, "remote_folder", ""))
        if folder:
            return folder

    # Shared/default server: paths are stored per purpose here.
    folder = _clean((getattr(row, "route_paths", None) or {}).get(purpose))
    if folder:
        return folder

    # Legacy fields are allowed only for the same semantic purpose.  They are
    # never cross-used (for example 835_IN can never become 837_IN).
    legacy_field = {
        "837_IN": "inbound_837_folder",
        "835_IN": "inbound_835_folder",
        "MIR_OUT": "outbound_mir_folder",
        "RECON_IN": "inbound_recon_folder",
    }.get(purpose)
    if legacy_field:
        return _clean(getattr(row, legacy_field, ""))
    return ""


def _select_in_scope(queryset, purpose):
    """Return (credential row, folder) from one client/global scope."""
    dedicated = queryset.filter(purpose=purpose).order_by("-updated_at").first()
    if dedicated and not dedicated.use_default:
        folder = _folder_from_row(dedicated, purpose)
        if folder:
            return dedicated, folder

    # If a purpose row says to use default, or no dedicated row exists, use
    # this scope's DEFAULT server and that purpose's route_paths entry.
    default = queryset.filter(purpose="DEFAULT").order_by("-updated_at").first()
    if default:
        folder = _folder_from_row(default, purpose)
        if folder:
            return default, folder

    # Transitional records may contain route_paths even when purpose != DEFAULT.
    for row in queryset.order_by("-updated_at"):
        folder = _clean((row.route_paths or {}).get(purpose))
        if folder:
            return row, folder

    return None, ""


def resolve_admin_sftp_route(client, purpose):
    """Resolve exactly the admin-configured connection and folder for purpose.

    Resolution order:
      1. selected client's dedicated/default configuration
      2. global administrator default configuration

    Returns (config, credentials, folder).  No unrelated folder fallback is
    permitted.
    """
    purpose = _clean(purpose).upper()
    if purpose not in VALID_PURPOSES:
        raise ValueError(f"Unsupported SFTP route purpose: {purpose}")

    config = folder = None
    if client is not None:
        config, folder = _select_in_scope(SFTPConfig.objects.filter(client=client), purpose)

    if not config or not folder:
        config, folder = _select_in_scope(SFTPConfig.objects.filter(client__isnull=True), purpose)

    if not config or not folder:
        raise ValueError(f"The admin-configured {purpose} SFTP route is missing.")
    if config.status != "CONNECTED":
        raise ValueError(f"The admin-configured {purpose} SFTP route is not connected (status: {config.status}).")

    credentials = get_sftp_runtime_credentials(config, outbound=purpose.endswith("_OUT"))
    if not credentials.get("host") or not credentials.get("username"):
        raise ValueError(f"The admin-configured {purpose} SFTP credentials are incomplete.")

    # Force the exact admin-selected folder into runtime credentials so all
    # downstream SFTP code uses this route and cannot fall back to 835/MIR.
    credentials = dict(credentials)
    credentials["remote_folder"] = folder
    credentials["purpose"] = purpose
    return config, credentials, folder
