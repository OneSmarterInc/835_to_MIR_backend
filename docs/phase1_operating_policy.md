# Phase 1 operating policy worksheet

Status: review required. Owner roles below must be assigned to named people.
The worksheet documents current behavior and decisions required; it is not a
runtime configuration file and does not introduce automatic deletion or holds.

## Upload limits and admission

| Route / boundary | Existing behavior | Proposed action / approval owner |
|---|---|---|
| Analysis per-file bytes | 10 MiB | Retain until representative load testing justifies a change; backend + product |
| Analysis claims/segments | 20,000 claims / 200,000 segments | Retain structural guards; measure parsing memory/time at boundaries; backend + QA |
| Main 837 portal | `EDI837_MAX_UPLOAD_BYTES`, default 100 MiB | Verify deployed value and peak memory before approving production maximum; infrastructure |
| Main 835, RECON, SFTP | Route-specific behavior; no single approved uniform intake budget | Inventory actual web proxy/application limits and remote file sizes; agree per-route limits before shared intake rollout |
| Multi-file submissions | Browser/API behavior varies by page; Analysis submits individual files | Preserve per-file outcomes; agree aggregate request and in-flight limits separately |
| Tenant routing | Selected/assigned client and configured SFTP purpose | Require approved sender/client mapping for new adapters; no auto-route from filename |

Numeric acceptance limits must identify unit, route, synchronous/asynchronous
handling, oversized-file response and responsible owner. A safe limit requires
observed bytes, claims, service counts, latency and memory; size alone is insufficient.
Do not lower limits globally during this documentation rollout.

## Retention classes

| Artifact class | Current known behavior | Decision and evidence required |
|---|---|---|
| Original 835/837/RECON sources | DB/source archive copies, route-specific lifecycle | Duration and start event; client offboarding behavior; retention owner; restore coverage |
| MIR outputs and manifests | DB + archive; delivered out copy removed | Preserve accounting/traceability obligations; define archive expiry separately from delivery cleanup |
| Analysis sources and normalized claims | Separate persistent DB tables | Explicit retention and approved future merge/deletion procedure; not a disposable browser cache |
| Rejected claims/files | Stored findings/source for some outcomes; unsafe envelope failures may be unsaved | Decide diagnostic/raw retention, correction/retry linkage and owner |
| Active queue jobs | Filesystem JSON until completion/failure | Never expire active work solely due to age; alert and investigate |
| Terminal queue jobs | Existing pruning after seven days | Approve or replace this duration; separate short-lived UI job status from long-lived audit evidence |
| Audit and security records | Existing audit/session/log mechanisms | Retention, access, tamper evidence and incident requirements per record class |
| Backups and recovery copies | Deployment configuration unverified | Backup schedule/retention, deletion propagation, encryption, restore evidence and key custody |
| Test/evidence artifacts | Synthetic repository fixtures; operational evidence may be restricted | No real patient data in public/shared fixtures; owners set evidence retention/access |

For each class, the approved record must contain duration (or explicit indefinite
retention), trigger date, storage locations, deletion mechanism, hold precedence,
approver and effective date. These values cannot be derived from repository code.

## Legal holds

Owner: legal/compliance with security and data operations. Define case/reference,
scope (client/receipt/artifact classes/date range), requester, authorized approver,
effective date, release authorization and audit trail. A hold must take precedence
over every applicable automatic/manual purge and relevant backup lifecycle.

A central enforced legal-hold facility is not implemented by this patch. Until
one is implemented and tested, do not claim that marking a document “on hold”
protects runtime files. Use the organization's approved preservation process and
verify coverage of existing seven-day terminal-job pruning if jobs are in scope.
Future hold tests must cover original, derived, Analysis, offboarding and backup
paths plus concurrent hold/purge changes. No retention purge is added here.

## Queue and recovery targets

`phase1_acceptance.json` keeps three numeric targets unset until agreed:

- `max_queue_age_seconds`: infrastructure + product agree the maximum wait and
  escalation response, based on peak observation and business deadline.
- `rpo_seconds`: business + infrastructure agree the tolerated data-loss window;
  zero is a valid requirement only with a demonstrated supporting recovery design.
- `rto_seconds`: business + infrastructure agree restoration time and the service
  scope that must be restored, including delivery reconciliation.

Use `phase1_queue_status` to observe age. A threshold is explicitly supplied by the
operator; none is silently presented as approved. Missing/malformed/oversized job
files or an incomplete scan yield unknown status, never a healthy zero. Queue age
includes intentionally delayed retries and is distinct from worker runtime.

Escalation procedure: alert the on-call owner; verify the web and batch services,
queue integrity, capacity, DB and remote dependencies; identify uncertain delivery
before replay. The command does not restart, cancel, purge or retry any job.

## Closure evidence

The operating-policy sign-off reference must include the upload/retention/hold
matrix, named owners, numeric targets and approved exception handling. Attach
capacity and isolated restore results separately. The readiness command checks
the presence of this record; it cannot authenticate a signature, verify an
external ticket, or certify that a policy has been implemented in production.
