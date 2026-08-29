# Django REST Framework API migration

## Compatibility strategy

The public API contract is intentionally unchanged. Existing URL paths still
resolve to the same business functions, but Django REST Framework `APIView`
adapters now own HTTP dispatch, parsers, authentication, permissions, exception
handling and JSON rendering. File and streaming responses pass through without
conversion. Existing services remain the only implementation of EDI validation,
conversion, MIR persistence, archive generation, SFTP and reconciliation.

This is a compatibility migration, not an endpoint redesign. The React
application therefore requires no URL, request-body or response-shape changes.

## Migration map

| Existing endpoint group | DRF boundary | Existing frontend callers | Request contract | Response contract |
|---|---|---|---|---|
| `/accounts/api/login/`, `signup/`, `logout/`, `user/` | Public `APIView` adapter; established Django session logic | `Login.jsx`, `AuthContext.jsx`, `DashboardLayout.jsx` | Existing JSON bodies and session cookie | Existing `success`, `authenticated`, user, MFA and offboarding fields |
| `/accounts/api/totp/*`, `contacts/`, `user/change-password/` | Authenticated `APIView` adapter | Authentication and client profile components | Existing JSON/POST fields | Existing success/error envelopes |
| `/accounts/api/admin/*` | Staff-only `APIView` adapter | Legacy admin client/user screens | Existing JSON and path IDs | Existing admin list/stat/user/client payloads |
| `/admin-panel/api/clients*`, onboarding, go-live, offboarding | Staff-only `APIView` adapter | Admin onboarding, go-live and offboarding screens | Existing JSON, multipart uploads and path IDs | Existing state/step/note/document payloads |
| `/admin-panel/api/users*`, `employee-roles`, `access/info` | Staff-only `APIView` adapter | Admin user and access screens | Existing JSON and path IDs | Existing list/detail/action payloads |
| `/admin-panel/api/mappings*`, `smtp`, `audit-logs` | Staff-only `APIView` adapter | Mapping, SMTP and audit screens | Existing JSON/query parameters | Existing mapping/config/log payloads; stored secrets remain write-only |
| `/api/validate/`, `/api/convert/`, `/api/start-batch-conversion/` | Authenticated multipart/form/JSON `APIView` adapter | Client and admin conversion screens | Existing EDI text, one/many 835 files, optional 837 and client scope | Existing validation reports, conversion results, IDs and canonical `mir_filename` |
| `/api/download/`, `/api/download-zip/`, `/api/file-content/{id}/` | Authenticated `APIView` adapter with streaming response passthrough | Conversion viewer, archive and offboarding archive download | Existing query/path parameters | Existing binary/text response, headers and JSON preview shape |
| `/edi835/api/tracked-files/`, `archive-files/`, `metrics/` | Authenticated `APIView` adapter | Client/admin conversion, archive, files and dashboard screens | Existing paging/filter/scope query parameters | Existing arrays, counts, metrics and paging fields |
| `/edi835/api/process/`, `/edi835/api/start-batch-conversion/` | Authenticated multipart/form/JSON `APIView` adapter | Flow and conversion workflows | Existing record/file identifiers and uploads | Existing processing/conversion result shape |
| `/edi835/api/sftp/*` | Authenticated `APIView` adapter | Client/admin SFTP configuration screens | Existing config JSON, browse/test/verify/push actions | Existing status, path, error and result fields |
| `/edi835/api/recon/*`, `/edi835/api/reconciliation/*` | Authenticated `APIView` adapter | Reconciliation screens and archive actions | Existing upload, paging, sort, search and record IDs | Existing file/claim/result payloads and downloads |

## Authentication and permissions

- Authentication continues to use Django sessions and cookies.
- JWT and a second token lifecycle were deliberately not introduced.
- The DRF authentication class reuses the user resolved by Django's existing
  authentication middleware.
- Existing MFA, administrator and offboarded-client middleware remains in force.
- Administrator endpoints additionally require an authenticated staff user at
  the DRF permission layer.
- Existing CORS and CSRF middleware/configuration remains authoritative.

## Parsers and renderers

Compatibility views accept `multipart/form-data`, form-encoded data and JSON.
JSON responses use DRF's `JSONRenderer`. Existing `FileResponse` and other
binary/streaming responses are returned intact so download behavior and
`Content-Disposition` headers do not change.

## Serializer exposure policy

Serializers are defined for clients, users, contacts, EDI 835 records, MIR
metadata and claims, reconciliation files, SFTP configuration, mappings,
documents, offboarding state, audit logs and SMTP configuration. Secret fields
are excluded, including passwords, TOTP secrets, recovery codes, SSH keys and
stored SFTP/SMTP passwords. Large database-backed file content is not included
in list serializers and remains available only through the established,
permission-protected preview/download endpoints.

## Canonical MIR filename contract

`MIRFile.mir_filename` remains the source of truth. API serialization, admin and
client conversion lists, MIR Built, archive, downloads and SFTP delivery must use
that field whenever it exists. Internal `output_path` values are not a substitute
for a canonical filename. Regression tests cover serializer and existing
download behavior.

## Error compatibility

DRF authentication and permission exceptions use the established envelope:

```json
{
  "success": false,
  "error": "Authentication required"
}
```

Errors already returned by an established endpoint retain their current body and
status code. Unexpected exceptions remain server-side logged and are not exposed
as stack traces.

## Production configuration

Install `requirements.txt`, add `rest_framework` to `INSTALLED_APPS`, and apply
the tracked `REST_FRAMEWORK` configuration from `localsettings.py` to the
environment-specific production settings module. No model or destructive data
migration is part of this API-layer change.

An authenticated staff health probe is available at `GET /api/drf/status/`.
