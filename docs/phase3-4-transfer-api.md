# Phase 3–4: transfer history and partner API

Implemented 22 October 2026. Migration: `edi835.0065_transfer_api_contract`.
This is an additive API over the existing private intake, dispatch worker and
processing attempts. Existing portal, Analysis, SFTP and conversion URLs retain
their existing behavior and response contracts.

## Delivered requirements

| Requirement | Implementation | Evidence / remaining verification |
| --- | --- | --- |
| Paginated unified history | `/api/v1/transfers`, client/status/channel/inclusive UTC date filters, 1–100 rows, stable timestamp/UUID ordering; client/channel/date and client/state/date indexes | API filter/pagination tests; portal history uses selected client and these filters |
| Lost-response/session recovery | Principal-scoped key lookup and idempotent resubmission; reauthenticate as the same staff account or use a valid credential for the same stable partner identity | Lost-response, expired-session and restored-session tests |
| Private downloads | Scoped source/output handlers, authorization every request, attachment/no-store/nosniff, SHA-256/length verification with bounded memory | Download, tamper, foreign-output and cross-client denial tests; production nginx/filesystem review still required |
| Audited eligible retry | Staff-only POST with reason; immutable retry audit links original attempt and next worker attempt | Safe retry, effects/attempt-limit denial, duplicate POST and audit linkage tests |
| Authorization/input matrix | Upload/list/detail/retry/download scope; session, MFA, CSRF, expired bearer, size, executable/archive content checks | `edi835.test_transfer_api`; PostgreSQL race tests added separately |
| Versioned durable intake | Multipart POST returns 202 only after source finalization and atomic receipt/key/dispatch commit | Commit rollback, real multipart Python-client/837-worker round trip |
| Transfer list/detail/retry contract | Versioned routes and UI detail/history controls | API and frontend integration tests |
| Atomic submission keys | `SubmissionKey.digest` primary key, receipt one-to-one; SHA-256 of API namespace + principal + caller key | Original-receipt recovery; no raw caller key stored |
| Conflicting/concurrent keys | Same principal/key with changed client/workflow/bytes gives 409; PostgreSQL nonblocking advisory transaction lock reports `SUBMISSION_IN_PROGRESS` | Conflict tests; simultaneous-key PostgreSQL test requires a PostgreSQL test server |
| Partner integration materials | Published OpenAPI, two synthetic X12 fixtures, streaming Python 3.12 client | Schema/fixture and Python multipart integration tests |

## API

Canonical routes omit the trailing slash. `/api/v1/transfers/` is also accepted
for the collection without redirecting a POST.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/transfers` | One multipart `file`, `workflow=835/837`, staff `client_id`; `Idempotency-Key` required |
| GET | `/api/v1/transfers` | Authorized history; optional `client_id`, `status`, `channel`, `date_from`, `date_to`, `page`, `page_size` |
| GET | `/api/v1/transfers/recover` | Recover by `Idempotency-Key` header without resending bytes |
| GET | `/api/v1/transfers/{receipt_id}` | State, validation, attempts, transaction coordinates, download links |
| POST | `/api/v1/transfers/{receipt_id}/retry` | Staff only; JSON `{"reason":"Upstream dependency restored"}` |
| GET | `/api/v1/transfers/{receipt_id}/source` | Authorized original source download |
| GET | `/api/v1/transfers/{receipt_id}/outputs/{output_id}` | Authorized output belonging to this receipt |
| GET | `/api/v1/transfers/openapi.json` | Public specification only, no client data |

OpenAPI source: `docs/openapi/transfers-v1.json`.
Partner credentials use `Authorization: Bearer <issued-uuid.secret>` over HTTPS.
They expire, can be revoked, bind one client and permitted workflows, and can read
only their own submissions. An invalid bearer token never falls back to a browser
session. Staff requests use the existing session, MFA policy, server-side client
grants and CSRF token (`csrf_token` from `/accounts/api/user/`). Demo/client-user
permissions are not expanded by this API.

202 means **durably received**, not validated, converted or delivered. Poll the
returned `status_url`. The receipt's `state` is the common lifecycle, distinct from
the legacy dispatch state. New duplicate-content submissions receive their own
202 `DUPLICATE` receipt linked to the canonical receipt. Replaying the same key
returns the original receipt with 200 and `reused=true`.

Keys are scoped to the authenticated principal, independent of client/workflow.
The existing legacy intake key contract is unchanged. V1 keys are new; there is
no invented key backfill for older uploads. Same key + same bytes + same client +
same workflow recovers the receipt. Original filename changes alone do not create
a conflict. Different content/routing gives `409 IDEMPOTENCY_CONFLICT`.
An unresolved concurrent submission gives `409 SUBMISSION_IN_PROGRESS` with
`Retry-After: 2`. Recovery GET can return 404 while the transaction is uncommitted;
it does not prove the original upload failed. Retry that upload with the original
key and unchanged file. Storage/database failures return 503, never a false success.

Keys remain for the retained lifetime of their receipt. No arbitrary expiry lets
a delayed retry silently convert the source again. Approved client deletion
cascades key/audit records through the existing client-deletion process. Credential rotation preserves the stable partner identity and key scope after
migration 0066. Recover with a valid replacement credential for that same partner.
Creating a different partner identity still creates a different key scope. See
[partner security rollout](phase4-partner-security.md) for mTLS and rotation.

## Retry safety

An operator can queue a retry only when the canonical receipt and dispatch have
failed, the dispatch is a one-source `INTAKE` job, no attempt is running, no attempt
recorded effects, no output exists, original client/partner authorization remains
valid, and queue/attempt limits permit it. Source integrity is checked first.
Inline/legacy/manual/SFTP dispatches retain their existing recovery procedures.
Rejected inputs need corrected files with new submission keys.

Receipt, retry audit and queued dispatch update commit atomically under locks.
Concurrent retry requests cannot enqueue two attempts. The reason and actor are
recorded before responding; when claimed, the new attempt links to the prior
attempt and retry audit. Reasons must not contain patient data or credentials.
Uncertain external delivery is never automatically resent. Reconcile it first.

## Partner example

Set `FILEX_BASE_URL` to the API HTTPS origin and `FILEX_PARTNER_TOKEN` through your
secret manager/environment. Do not commit credentials or include them in shell
history. The sample uses the Python standard library and verified TLS, transmits
1 MiB chunks, applies a socket timeout and never automatically replays a POST.

```bash
python examples/transfer_client.py --file examples/synthetic_claim.837 --workflow 837 --key synthetic-837-run-001
python examples/transfer_client.py --recover --key synthetic-837-run-001
python examples/transfer_client.py --status RECEIPT_UUID
python examples/transfer_client.py --file examples/synthetic_payment.835 --workflow 835 --key synthetic-835-run-001
```

Use a synthetic/staging client with outbound delivery disabled or routed to a
test destination. The examples deliberately contain synthetic identifiers and
X12 test indicators; a test indicator alone is not an outbound-send safeguard.
Retain a key before uploading. Use a new key for a deliberately new submission.

## Deployment and checks

Apply migration 0065 before deploying the frontend. No new worker/service is
needed: both existing Django and batch worker processes must run the new code.
PostgreSQL is the supported production concurrency boundary; SQLite tests do not
prove advisory/row-lock behavior. The migration adds tables and history indexes;
plan index creation during maintenance according to the production receipt volume.

On a dedicated staging database, run:

```bash
python manage.py migrate --noinput
python manage.py check
python manage.py test edi835.test_transfer_api edi835.test_intake_postgres --noinput
python manage.py intake_readiness
```

Use a database role permitted to create a **test database**. Never point tests or
synthetic uploads at a live client. Repository-wide synthetic regression:

```bash
python scripts/run_phase1_checks.py --report /tmp/phase3-4-regression.json
```

Frontend verification:

```bash
node scripts/test-intake-history.js
npm run test:csrf
node scripts/test-upload-request.js
npm run test:client-analysis
npm run build
```

Production application sequence after the approved maintenance/drain procedure:

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
git pull --ff-only origin main && python manage.py migrate --noinput && python manage.py check && python manage.py intake_readiness && sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

Review `deploy/intake-nginx.conf.example` in the existing API server block; test
with `sudo nginx -t` before reloading. Verify the actual gateway rejects oversized
requests, `/media/` and guessed filesystem paths are not publicly served, the
private intake directory is not aliased by nginx/CDN, TLS/MFA are enforced, and
partner credentials are appropriately provisioned. The Django DEBUG public media
mount is removed; this cannot change an independently configured nginx alias.
Back up receipt/key/audit tables and private sources/outputs together.

## Tradeoffs and limits

Verification for this change: 412 backend tests ran, 406 passed and six were
skipped (PostgreSQL-only concurrency/precision checks). Evidence is recorded in
`docs/phase3-4-transfer-regression.json`. Migration drift check passed. The frontend
history integration, Analysis integration, 11 CSRF/request-stack tests, four upload
transport tests, targeted lint and production build passed. The build retains its
existing large-bundle and MappingPreview CommonJS warnings. No AWS deployment or
production data mutation was performed.

- Integrity verification adds one sequential file read before a download begins;
  it uses bounded memory and prevents serving a known-corrupt attachment.
- Offset pagination provides exact totals and simple navigation; new arrivals can
  shift pages. It does not promise a frozen export snapshot.
- Only side-effect-free failures are retryable through this API. Broader retries
  need workflow-specific reconciliation rather than risking duplicate delivery.
- History includes registered receipts, not fabricated historical events for files
  that predate shared intake. Existing portal upload paths keep their existing
  recovery behavior; key recovery applies to submissions made through V1.
- Finalized objects left after a rolled-back/uncertain commit remain private and
  are handled by the existing intake reconciler, never deleted speculatively.
- Production PostgreSQL races, nginx limits/private paths and deployment-specific
  session/MFA behavior require staging/operational verification before release.
