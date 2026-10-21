# Phase 1 architecture decision records

21 October 2026. Status: technical recommendations prepared for review. Formal
approval remains pending in `phase1_acceptance.json`. These records approve no
partner, storage deletion, retention duration or production recovery claim.

## ADR-001 — Retain Django/PostgreSQL and the existing converter

Owner: technical lead; reviewers: backend, QA, infrastructure.

Decision proposed: implement shared intake within the existing Django backend,
reuse the current MIR parser/mapper/generator, and retain PostgreSQL. No new
microservice, Redis or Celery dependency is required merely to complete Phase 1.
Analysis remains in its own client-scoped tables until a separate merge is approved.

Why: tenant/authentication, structured claim storage and the converter already
exist. Replacing them together with intake would expand both migration and
regression risk. Existing worker operations also include releases and alerts.

Alternatives: a separate intake service adds deployment and distributed-auth
boundaries; Celery can offer mature scheduling/retry tooling but does not itself
solve database/dispatch atomicity or duplicate remote delivery.

Tradeoff: improving the current worker requires explicit ownership, recovery and
observability design. Review this decision after measured queue/load evidence
shows a need for multiple workers or independent scaling.

Implemented now: exclusive file lock before batch-worker startup recovery and
processing. This enforces one cooperating worker per queue directory. It is not
a multi-host lease or an exactly-once delivery guarantee. All running workers must
be upgraded; an old worker does not participate in the lock protocol. Use local
storage with working OS advisory locks; do not assume equivalent behavior on NFS.

## ADR-002 — Private durable originals, structured DB records and output manifests

Owner: backend lead; reviewers: infrastructure and security.

Decision proposed: retain original bytes immutably per receipt, store normalized
fields in PostgreSQL, and identify source/output objects through manifests with
client, digest, byte size and receipt/attempt identifiers. Display original names;
do not use filenames as business identity. For initial deployment retain the
existing client-separated local storage behind an adapter. Object storage is a
future adapter, not a prerequisite selected without capacity/hosting evidence.

Write protocol: stream to private temporary storage; hash and size while writing;
finalize durable bytes; commit receipt and dispatch intent atomically in DB. A
reconciler handles orphan bytes and missing-object receipts with an age grace
period and explicit audit. Never delete a recently active upload automatically.

Alternatives: DB-only binary storage simplifies a single transaction but increases
database/backup volume; filesystem-only records lose relational constraints and
searchability. Local storage keeps deployment simple but ties durability and
capacity to the host and requires coordinated backup. No S3 durability claim is
made for the current local filesystem.

Migration: additive receipt/object references; existing paths stay readable;
historical registration is separately scoped and never dispatches old files by
default. Analysis storage remains unchanged. No new deletion policy is enabled.

## ADR-003 — Database dispatch intent and atomic worker claims

Owner: backend lead; reviewers: technical lead and QA.

Decision proposed for Phase 2: create receipt, processing-attempt and dispatch-intent
rows in the same database transaction. Claim work atomically with bounded leases;
renew ownership during long processing; reconcile expired claims. Persist retry
classification, next eligible time and attempt count. Dispatch should be at least
once, with business idempotency and delivery reconciliation.

Current risk: JSON queue replacement is atomic for a file, but queue creation and
DB state are separate operations. A singleton worker reduces concurrent execution
risk; it does not close this dispatch gap or prove delivery exactly once.

Alternatives: continuing only file scans is simpler but cannot atomically commit
DB and queue state; a broker without a transactional outbox retains that gap.

Tradeoff: a DB queue adds schema, indexes and operational queries. Keep lock
transactions short; never hold a DB row lock through parsing or SFTP transfer.
Worker ownership, recovery and ordering require PostgreSQL concurrency tests.

## ADR-004 — Explicit transport trust and partner/client identity

Owner: security/infrastructure; reviewers: integration lead.

Decision proposed: HTTPS uses validated certificates with monitored renewal; SFTP
uses approved server host keys and explicit route purposes. An unknown or changed
host key must enter a controlled verification/rotation procedure. Any future
partner mTLS identity maps to explicit clients and transaction permissions; a
filename or unverified sender field must not grant tenant access.

No blanket runtime trust change is shipped here: existing trust configuration and
partner host keys must be inventoried first to avoid an unplanned transfer outage.
The codebase contains trust-on-first-use behavior; record each affected route,
verify fingerprints through an independent approved channel, stage pinned keys,
test the connection, then remove exceptions under a reviewed rollout.

Tradeoff: strict trust requires credential/key lifecycle operations. Silent
acceptance is operationally convenient but cannot establish server identity.
Certificate/key expiry, replacement, revocation and emergency owner contact must
be included in the integration record. Never store private keys in this repository.

## ADR-005 — Reconcile uncertain outcomes before replay

Owner: infrastructure and backend leads; reviewers: integration lead, QA.

Decision proposed: distinguish permanent validation errors, transient failures
and uncertain remote delivery. Retry only under the relevant policy; preserve
source and attempt history. A remote success followed by local failure must be
reconciled using output identity/partner receipt before sending again.

Recovery sequence: restore isolated DB, bytes, configuration and keys; keep workers
and external delivery off; compare receipt/output manifests to the restored objects
and partner delivery evidence; classify queued/running work; only then resume.

Alternatives: automatic replay of every interrupted job is simpler but can duplicate
payments/files; marking every interruption complete can lose work. Current startup
recovery marks running jobs failed, which is neither of those guarantees.

Tradeoff: uncertain cases may require operator review and slow recovery. Business
RPO/RTO, alert thresholds and partner acknowledgement semantics remain explicit
approval dependencies in the operating policy.

## Approval procedure

Review these decisions together with the flow map, policy and migration plan.
For each unresolved exception record the affected feature, accountable named owner
and acceptance criterion. Store sign-off in a controlled ticket/document; update
the acceptance record with reviewer, date and reference. A generated test report
is evidence, not the reviewer's approval. Major design changes need a superseding
decision with rollout/backward-compatibility implications documented.
