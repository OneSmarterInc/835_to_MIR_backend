# Phase 1 implementation and acceptance guide

This package addresses the four follow-up gaps: architecture decisions, operating
policy, migration/fixtures/estimate, and an explicit acceptance gate.

| Deliverable | Implemented now | Still requires accountable-owner evidence |
|---|---|---|
| Architecture | Five [decision records](phase1_decisions.md), alternatives/tradeoffs and staged design; worker ownership lock | Formal approval; actual host keys/certificates, storage and recovery configuration |
| Operating policy | [Policy matrix](phase1_operating_policy.md), current limits, retention/hold requirements and queue observation command | Approved retention/holds, numerical queue/RPO/RTO targets and implementation of future policy enforcement |
| Readiness for implementation | [Migration/rollback plan, backlog and revised estimate](phase1_delivery_plan.md); synthetic converter demo; isolated regression runner | Named assignment, partner corpus, PostgreSQL/staging rollout and restore rehearsal |
| Acceptance | Versioned approval/evidence record and fail-closed completeness checker | Actual reviewers, dated sign-offs and external operational evidence |

## Repeatable local verification

From the backend directory in its prepared virtualenv:

```bash
python scripts/run_phase1_checks.py --report phase1-tests.json
python manage.py phase1_converter_demo
python manage.py phase1_readiness
```

The test runner isolates SQLite and media, uses synthetic fixtures and writes
optional aggregate test evidence. The converter demo needs application dependencies
and configured Django startup but performs no DB query/write or external delivery.
It uses default mappings, not a particular client's saved configuration.

`phase1_local_verification.json` records the local test outcome, tested source
fingerprint, repository parent revision and whether changes were uncommitted.
The parent revision is not presented as the commit containing these changes.
The fingerprint covers application Python, the synthetic 835, runner, requirement
file and acceptance record. Test logs/counters are not production approval.

Verified locally on 21 October 2026: **254 tests passed, 1 PostgreSQL-only precision
test skipped, no failures/errors**. The standalone converter demo passed all nine
checks; its source/output digests and scope are in
[`phase1_converter_evidence.json`](phase1_converter_evidence.json). Django system
checks passed and `makemigrations --check --dry-run` found no model changes.
Worker lock tests include contention from a second OS process and release after
an exception. Tests ran on Windows/SQLite; Linux deployment and PostgreSQL checks
remain required. The two corrected legacy test expectations now verify structured
duplicate-release state and unsupported PR77, while existing PR31 support stays
unchanged.

## Queue observation

```bash
python manage.py phase1_queue_status
```

For monitoring, use the agreed value from the approved policy:

```bash
python manage.py phase1_queue_status --max-queue-age-seconds "$APPROVED_QUEUE_AGE_SECONDS" --fail-if-unhealthy
```

The command scans at most 5,000 job files, reading at most 1 MiB per file, one at a
time. `--max-files` can be increased up to 100,000 deliberately. Missing directories,
invalid timestamps, unreadable/oversized records and scan truncation produce unknown
status; strict mode exits nonzero. It emits counts/ages only. It does not create the
queue directory, reveal names/payloads, or retry/delete jobs. This is not a heartbeat
check; verify both services separately. Delayed retries count toward queued age.

## Acceptance procedure

1. Assign named owners to the roles in the ADRs, policy and delivery plan.
2. Run and review baseline, PostgreSQL, partner, capacity, restore, trust and rollback
   evidence. Store operational evidence in restricted storage; never commit secrets
   or real patient data. Use a controlled ticket/document reference in the registry.
3. Review and sign the architecture, operating policy, migration plan, converter
   demonstration and overall Phase 1 acceptance. Each approved/passed entry requires
   `owner_name`, `recorded_on` (ISO date) and `evidence_reference`.
4. Enter agreed integer queue-age, RPO and RTO targets; no values are pre-approved.
5. Run `python manage.py phase1_readiness --require-approved`. It exits nonzero until
   required entries are complete. `--record PATH` supports a private operations copy.

The checker validates completeness and dates, not authenticity of sign-offs or
truth of external evidence. `recorded_gate_complete` deliberately does not mean
“production certified”. The committed record remains pending; do not replace
reviewers with a script or mark a skipped PostgreSQL test as passed.

## Deploying the changes in this package

No database migration or frontend change is introduced. The existing `filelock`
dependency is now explicit in `req.txt`; verify it is available in the deployment
virtualenv. The repository's broader dependency-file reproducibility still needs
separate cleanup; `req.txt` includes OS-specific packages and is not a portable
fresh-environment lockfile.

Drain active batch work before restart; startup recovery still marks interrupted
jobs failed. Upgrade every worker, then restart both services through the normal
release procedure:

```bash
python manage.py check
sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

A second updated worker sharing the same local queue directory now exits before
recovery/processing instead of interfering with the running worker. A stale empty
`.worker.lock` file is harmless: ownership is an OS lock released on process exit;
never delete a lock file to bypass a live worker. Different directories/hosts and
older worker versions are outside this guarantee. Database dispatch atomicity,
distributed leases and uncertain-delivery reconciliation remain Phase 2 work.

Rollback: drain work and restore the previous reviewed application revision; there
is no schema reversal. Observe that rollback also removes singleton enforcement.
No AWS deployment or production policy mutation is performed by this package.
