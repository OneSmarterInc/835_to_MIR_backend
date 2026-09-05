"""Conversion Test wrapper with strict 837 route resolution.

The shared SFTP resolver mutates route fields on its returned model instance for
runtime compatibility.  For a DEFAULT/UNIFIED connection that can make the
837 relay inherit the generic 835 remote_folder.  This module deliberately
re-reads the persisted row before choosing the 837 route so Test always uses
the administrator-saved 837_IN / 837_OUT path.
"""

from . import batch_test_837_v2 as _v2
from .models import SFTPConfig


def _raw_effective_route_folder(config, purpose, credentials):
    """Return the persisted purpose-specific route without 835/MIR fallback."""
    if not config:
        return ""

    try:
        raw = SFTPConfig.objects.get(pk=config.pk)
    except (SFTPConfig.DoesNotExist, ValueError, TypeError):
        raw = config

    route_paths = getattr(raw, "route_paths", None) or {}
    routed = str(route_paths.get(purpose) or "").strip()
    if routed:
        return routed

    row_purpose = str(getattr(raw, "purpose", "") or "").upper()
    if row_purpose == purpose:
        dedicated = str(getattr(raw, "remote_folder", "") or "").strip()
        if dedicated:
            return dedicated

    # Legacy/default rows have a dedicated inbound 837 field.  Read it from
    # the fresh DB object so resolve_sftp_config cannot overwrite it in memory.
    if purpose == "837_IN":
        legacy = str(getattr(raw, "inbound_837_folder", "") or "").strip()
        if legacy:
            return legacy

    # 837_OUT intentionally has no MIR fallback.  It must be present in
    # route_paths or on a purpose-specific 837_OUT row.
    if purpose == "837_OUT":
        return ""

    return str((credentials or {}).get("remote_folder") or "").strip()


# _relay_837_for_test resolves this symbol from the v2 module at call time.
_v2._effective_route_folder = _raw_effective_route_folder
api_start_batch_conversion_with_837 = _v2.api_start_batch_conversion_with_837
