"""837 Search transfer entry point.

Authorization is enforced by the transfer service itself. Never modify a
user's role to reuse an administrator code path.
"""

from django.views.decorators.csrf import csrf_exempt

from .edi837_naming_views import edi837_sftp_transfer_named


@csrf_exempt
def edi837_sftp_transfer_for_search(request):
    """Delegate Search rename requests without re-enabling Django CSRF checks.

    ``edi837_sftp_transfer_named`` is already an authenticated, CSRF-exempt API
    view. Because this Search-specific wrapper is the callable registered in
    ``urls.py``, Django's CSRF middleware evaluates the wrapper itself before
    the delegated view is reached. Mark the wrapper exempt as well so the API's
    existing session/token authentication can run and return its JSON response.
    """
    return edi837_sftp_transfer_named(request)
