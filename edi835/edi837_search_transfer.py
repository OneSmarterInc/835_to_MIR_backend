"""837 Search transfer entry point.

Authorization is enforced by the transfer service itself.  Never modify a
user's role to reuse an administrator code path.
"""

from .edi837_naming_views import edi837_sftp_transfer_named


def edi837_sftp_transfer_for_search(request):
    return edi837_sftp_transfer_named(request)
