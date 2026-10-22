"""Audit legacy authenticated downloads without logging paths, payloads or tokens."""
import uuid

from django.http import FileResponse
from .intake_access import audit_access


class DownloadAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        authenticated = getattr(getattr(request, 'user', None), 'is_authenticated', False)
        if request.method == 'GET' and authenticated and response.status_code < 400:
            download = isinstance(response, FileResponse) or response.get('Content-Disposition', '').startswith('attachment')
            if download and not getattr(request, 'intake_request_id', None):
                match = getattr(request, 'resolver_match', None)
                identifiers = {}
                for name, value in (getattr(match, 'kwargs', {}) or {}).items():
                    if isinstance(value, (uuid.UUID, int)):
                        identifiers[name] = str(value)
                try:
                    audit_access(request, 'LEGACY_FILE_DOWNLOAD', endpoint=getattr(match, 'view_name', ''),
                                 resource_ids=identifiers, stage='AUTHORIZED')
                except Exception:
                    response.close()
                    raise
        if getattr(request, 'intake_request_id', None):
            response['X-Request-ID'] = request.intake_request_id
        return response
