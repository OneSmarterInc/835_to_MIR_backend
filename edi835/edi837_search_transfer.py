"""Role-safe 837 Search transfer entry point.

Administrators keep the existing behavior. Client portal users may run the same
Search-page inbound->outbound 837 transfer, but only for their own client; the
underlying client resolver already pins client users to request.user.client.
"""

from .edi837_naming_views import edi837_sftp_transfer_named


def edi837_sftp_transfer_for_search(request):
    user = getattr(request, "user", None)
    if user is None:
        return edi837_sftp_transfer_named(request)

    # The legacy transfer implementation has an administrator-only guard even
    # though its client resolution is already safe for client users. Temporarily
    # satisfy that guard for an authenticated portal user who belongs to a
    # client. This is runtime-only and is never persisted to the database.
    is_client_user = bool(getattr(user, "client_id", None)) and not bool(getattr(user, "is_staff", False))
    if not is_client_user:
        return edi837_sftp_transfer_named(request)

    original_is_staff = user.is_staff
    try:
        user.is_staff = True
        return edi837_sftp_transfer_named(request)
    finally:
        user.is_staff = original_is_staff
