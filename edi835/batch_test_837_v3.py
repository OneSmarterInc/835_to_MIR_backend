"""Conversion Test wrapper with strict 837 route resolution.

The generic SFTP resolver is intentionally compatible with older 835/MIR
configuration rows and may expose ``remote_folder`` as an effective runtime
folder.  That is unsafe for the 837 relay because a DEFAULT/UNIFIED row can
carry a legacy 835 path such as ``/835in``.

This module resolves 837_IN and 837_OUT directly from persisted purpose-specific
configuration.  A 837 Test transfer is never allowed to fall back to an 835 or
MIR folder.
"""

from . import batch_test_837_v2 as _v2
from .models import SFTPConfig


def _clean(value):
    return str(value or "").strip()


def _route_from_rows(queryset, purpose):
    """Find one explicit persisted route for purpose in a configuration scope."""
    # A dedicated purpose row is authoritative when it owns its connection.
    dedicated = (
        queryset.filter(purpose=purpose, use_default=False)
        .order_by("-updated_at")
        .first()
    )
    if dedicated:
        route = _clean(dedicated.remote_folder)
        if route:
            return route
        route = _clean((dedicated.route_paths or {}).get(purpose))
        if route:
            return route

    # For a shared/default server, every transfer route is stored in
    # route_paths.  Never use DEFAULT.remote_folder here: on legacy installs
    # that field commonly contains the 835 inbound path.
    default = queryset.filter(purpose="DEFAULT").order_by("-updated_at").first()
    if default:
        route = _clean((default.route_paths or {}).get(purpose))
        if route:
            return route

    # Some transitional records carried route_paths on non-default rows.
    for row in queryset.order_by("-updated_at"):
        route = _clean((row.route_paths or {}).get(purpose))
        if route:
            return route

    return ""


def _raw_effective_route_folder(config, purpose, credentials):
    """Resolve 837 route from persisted purpose data only; no 835/MIR fallback."""
    if purpose not in {"837_IN", "837_OUT"}:
        return _clean((credentials or {}).get("remote_folder"))

    client_id = getattr(config, "client_id", None) if config else None

    # First use the selected client's own configuration.
    if client_id:
        route = _route_from_rows(SFTPConfig.objects.filter(client_id=client_id), purpose)
        if route:
            return route

    # Then allow the administrator-managed global/default SFTP configuration.
    route = _route_from_rows(SFTPConfig.objects.filter(client__isnull=True), purpose)
    if route:
        return route

    # Last chance: an explicitly selected dedicated row.  This supports a
    # detached config object without ever accepting a DEFAULT remote_folder.
    if config:
        try:
            raw = SFTPConfig.objects.get(pk=config.pk)
        except (SFTPConfig.DoesNotExist, ValueError, TypeError):
            raw = config
        if _clean(getattr(raw, "purpose", "")).upper() == purpose:
            route = _clean(getattr(raw, "remote_folder", ""))
            if route:
                return route
        route = _clean((getattr(raw, "route_paths", None) or {}).get(purpose))
        if route:
            return route

    # Deliberately do NOT use inbound_837_folder, remote_folder,
    # inbound_835_folder, or outbound_mir_folder as a compatibility fallback.
    # If the 837 route was not explicitly saved, Test must report that instead
    # of silently polling the wrong folder.
    return ""


# _relay_837_for_test resolves this symbol from the v2 module at call time.
_v2._effective_route_folder = _raw_effective_route_folder
api_start_batch_conversion_with_837 = _v2.api_start_batch_conversion_with_837
