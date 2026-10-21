# Shared intake: migration plan, backlog and revised estimate

Scope: migration of current portal/SFTP intake to shared receipt, dispatch and
delivery contracts while retaining the existing converter and client authorization.
Analysis remains isolated. This document is a proposed implementation plan; the
shared intake service and migrations described below are not shipped by Phase 1.

## Preconditions and fixture coverage

Record the current revisions, configuration, effective services and outputs.
Run `python scripts/run_phase1_checks.py --report PATH` in a prepared backend
virtualenv. The runner forces an isolated in-memory SQLite test database and
temporary media directory; it does not use the configured production database.
Run PostgreSQL migration/concurrency tests separately in staging.

The committed `edi835/fixtures/phase1/synthetic_paid.835` contains fabricated
identifiers, names and amounts, marked test usage in ISA15. It exercises the real
converter with a fixed 1 October 2026 processing date, one claim/service, billed
200.00, paid 150.00 and patient responsibility 50.00. Expected MIR length is 637
characters per row (334 header + 303 service). The demo checks literal monetary
positions and emits source/output hashes without printing source or MIR content.

Run `python manage.py phase1_converter_demo` to demonstrate that narrow baseline.
This does not validate a full partner guide, use tenant-custom mappings, persist
a conversion, or send anything. Additional synthetic tests cover partial/invalid
Analysis claims, signed amounts, duplicate detection, unsupported PR mapping,
held claims, service splitting, tenant isolation, delivery ordering and CSRF.
The fixture inventory in `phase1_compatibility.md` identifies missing partner and
infrastructure scenarios. Never replace these fixtures with raw patient files.

## Staged rollout and rollback

| Stage | Work and acceptance | Rollback / stop condition |
|---|---|---|
| 0: baseline | Capture receipts/output examples, fixture hashes, configuration and effective revisions; restore backup in isolation | Stop if baseline outputs, client routing or recoverability are unexplained |
| 1: additive schema | Add receipt, object manifest, processing attempt, dispatch intent and delivery intent; tenant FKs, unique idempotency keys and query indexes | Old readers continue working; code rollback leaves new tables intact. Do not reverse/drop tables containing pilot data |
| 2: shared registration | Stream/hash/finalize bytes; register receipt and outbox in one DB transaction; reject ambiguous routing | Keep feature flag off for production; reconcile orphan storage using a safe grace period; no automatic historical processing |
| 3: worker and recovery | Atomic claims/leases, heartbeat, bounded classified retries and delivery reconciliation | Disable new intake/dispatch, drain or explicitly classify active attempts; never let old worker consume new ownership records |
| 4: portal pilot | Enable selected client/route; one authoritative dispatch path; compare DB counts, hashes, amounts and outputs to baseline | Disable flag for new arrivals; retain and finish/reconcile already accepted new receipts before old path resumes |
| 5: SFTP pilot | Explicit readiness protocol, sender/client mapping, pinned trust and partner sandbox; delivery manifest/acknowledgement | Pause polling, retain inbound, reconcile any uncertain sends before replay; no simultaneous old/new polling of same folder |
| 6: controlled expansion | Increase client coverage after observed queue/latency/error/duplicate targets pass | Roll back per client/route; maintain source/output linkage and audit evidence |
| 7: retire legacy dispatch | Reconciliation complete, acceptance signed, retention/backup coverage established | Retain legacy read compatibility and documented recovery until its approved retirement date |

Proposed model responsibilities: receipt owns client, source identity and acceptance;
object manifest owns path/key, hash, size and lifecycle; attempt owns processing
state, error category and timing; dispatch intent owns eligibility/lease; delivery
intent owns destination/output identity and acknowledgement. A unique content hash
alone is not universal business deduplication: preserve route-specific replay and
changed-claim rules until owners approve a consolidated contract.

Historical data migration defaults to metadata registration only. Use bounded,
resumable client-scoped batches with dry-run counts and checkpoints. Do not infer
delivery from a generated file or automatically resend historical sources. Compare
counts and hashes before/after; quarantine ambiguous links for review. Schema/index
migrations must be rehearsed on PostgreSQL at representative scale, with lock
impact and rollback steps recorded.

## Assigned-role backlog and effort

Estimates are engineering judgement for the shared-intake migration, not measured
throughput or a delivery promise. Assign named owners before kickoff.

| ID | Work | Accountable role | Dependency / acceptance | Developer-days |
|---|---|---|---|---:|
| IN-01 | Receipt/attempt/outbox schema and constraints | Backend lead | Approved ADRs; fresh + upgrade + rollback-compatibility tests | 3–5 |
| IN-02 | Shared registration and storage adapter | Backend engineer | IN-01; stream limits, immutable originals, DB/bytes failure injection | 4–6 |
| IN-03 | Atomic worker lease/retry/recovery | Backend lead | IN-01; PostgreSQL multi-worker and crash tests; no lost dispatch | 4–6 |
| IN-04 | Portal adapter and progress compatibility | Fullstack engineer | IN-02/03; auth/CSRF, per-file outcomes, UI refresh regression tests | 3–4 |
| IN-05 | SFTP readiness, routing and trust migration | Backend + integration lead | IN-02/03 and partner keys; sandbox transfer/replay tests | 4–6 |
| IN-06 | Delivery manifest/reconciliation | Backend engineer | IN-03/05; remote-success/local-failure case resolved without blind resend | 3–5 |
| IN-07 | Historical registration, pilot and rollout tooling | Release/backend lead | IN-04/05/06; resumable dry run, reconciliation and rollback rehearsal | 3–5 |

Total: **24–37 developer-days**, plus **8–12 QA-days** and **4–6 infrastructure-days**.
Some work overlaps, but dependencies and partner access prevent simply dividing
the total by headcount. Planning range: **5–8 calendar weeks** with two available
developers, shared QA/infrastructure and timely partner access. This replaces an
unqualified date with an explicit planning range; it is pending team approval.

Excluded from this range: new public partner API/mTLS onboarding, a central legal
hold product, bulk remediation of historical invalid files, a cloud storage
migration, unavailable partner access and major converter rule changes. Those need
separate estimates if approved. Re-estimate after Phase 1 operational measurements,
after schema/registration proof, and after the first end-to-end SFTP pilot. Record
actual effort and unresolved findings at each checkpoint.

## Phase 1 exit

Technical lead and product owner review the flow map and ADRs; infrastructure and
security review deployed trust/restore; QA demonstrates the converter baseline;
integration lead confirms partner fixtures and routing. Each pending item has an
owner and affected feature gate. Complete the controlled acceptance record only
after the evidence exists. Local green tests permit further development, not a
claim that production capacity, recovery or legal retention has been approved.
