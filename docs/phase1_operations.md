# Phase 1: baseline collection and recovery verification

This runbook collects evidence; it does not manufacture a production capacity
rating from local tests. Use a deployment with current migrations and the same
environment used by the services. Do not print `.env`, credentials or source data.

## Database baseline

From the backend directory on AWS, after deploying this change:

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
umask 077
python manage.py phase1_baseline --days 30 --include-host > phase1-baseline.json
```

For one client, add `--client-id CLIENT_UUID`. To repeat an exact historical window,
add e.g. `--as-of 2026-10-21T00:00:00+00:00`. The interval includes the start and
excludes the end. An invalid/missing client fails rather than silently selecting
all tenants. Do not paste shell placeholders without replacing them.

The default reads metadata aggregates only. The optional command below also
calculates 835/Analysis source sizes inside the database, without fetching payloads
into the command process. Run off peak or against an approved replica/snapshot:

```bash
python manage.py phase1_baseline --days 30 --include-content-sizes > phase1-sizes.json
```

PostgreSQL collection runs in one read-only repeatable-read transaction. Each SQL
statement has a default 15-second timeout; configurable `--statement-timeout-ms`
is bounded to 100–120000 ms. A timeout fails the command instead of returning a
misleading complete report. Redirected output may be empty on failure: check the
exit code before treating the JSON as evidence. The timeout is per statement, not
a total command deadline. Keep the window small on large installations.

The result includes eight separate streams: main 835, main 837, RECON, legacy 837
references, MIR outputs, Analysis 835, Analysis 837, and automation runs. Do not sum
these as “files received”: they include inputs, outputs, staged copies and jobs.

Interpretation:

- `records` counts persisted rows; combined 835 batches may represent several
  physical files. Unsaved rejected arrivals are absent.
- Peaks are UTC clock-hour/calendar-day buckets, not rolling peaks or requests/sec.
- `recorded_claims` sums stored counters, not unique business claims.
- Missing sizes/times are `null`, not zero. Zero/default stored sizes are counted
  separately; metadata size fields may be missing in legacy records.
- Processing intervals use the named DB timestamps. Some 835 timestamps include
  queue wait and delivery; 837 uses receipt-to-processed time. Analysis has no
  duration timestamps. Do not call these isolated conversion benchmarks.
- Host CPU/disk values describe the machine running the command, not necessarily
  database storage, container quotas, effective worker memory, or available capacity.
- Snapshot history can change on retries/reprocessing. Record deployed revision,
  collection time and window alongside each result.

No filenames, patient names, claim IDs, client UUIDs, credentials or source contents
are emitted. Aggregate operational evidence still belongs in restricted storage.

## Deployed service evidence

These read-only commands inspect both services without printing their environment:

```bash
sudo systemctl is-active mir.service mir-batch-worker.service
sudo systemctl show mir.service mir-batch-worker.service \
  --property=Id,ActiveState,SubState,MainPID,MemoryCurrent,MemoryPeak,MemoryHigh,MemoryMax,TasksCurrent,NRestarts
df -h /var/www/835_to_MIR_backend
free -m
```

Properties unavailable on the installed systemd version may be blank. Verify the
effective working directory, application revision, worker count and bind/socket
privately; do not distribute full environment/unit output containing secrets.
Confirm a single batch worker before testing recovery. Observe worker heartbeat
and queue age from job metadata in a restricted operational session; current
`/health/` checks DB/schema only. Do not export raw job JSON, which may contain
source names and results. `python manage.py phase1_queue_status` now provides
bounded counts and ages without exporting those payloads; see
[the readiness guide](phase1_readiness.md) for explicit-threshold monitoring.
Capture load metrics over a real peak window, not just a one-time snapshot.

## Representative load test — staging only

1. Provision an isolated PostgreSQL database and filesystem with synthetic or
   approved de-identified fixtures. Disable real outbound SFTP/email; use a sandbox
   SFTP server. Preserve production worker/resource settings for comparison.
2. Select small, typical, high-percentile and maximum observed input sizes/claim
   counts from the baseline. Cover 835, 837, RECON and Analysis separately.
3. Replay a representative normal arrival pattern, then the measured peak pattern.
   Measure enqueue, worker start, parse completion, persistence and delivery
   separately. Include queue depth/oldest age, p50/p95/p99 latency, throughput,
   CPU/RSS, disk pressure, DB query latency/locks, error and retry counts.
4. Inject worker termination, DB/remote unavailability, full disk, duplicate replay
   and a source still being uploaded. Reconcile every accepted source to DB rows,
   archive and expected output; ensure a retry cannot silently send a duplicate.
5. Record the highest sustained workload meeting agreed latency/error/resource
   criteria and recovery time. Define numeric upload limits, maximum queue age
   and scaling triggers from those results. Do not extrapolate unlimited capacity.

Approval record: workload/hash, revisions, environment, sample size, duration,
arrival rate, observed percentiles/resource peaks, expected/actual counts, failures,
approver and date. Product and infrastructure must supply acceptable targets;
they are currently unapproved. This change does not run a load generator on AWS.

## Backup and restore inventory

Verify the following with the infrastructure owner before claiming recoverability:

| Artifact | Required evidence |
|---|---|
| PostgreSQL | Backup/PITR mechanism, last successful backup, retention, restore permissions and last tested recovery point; includes main, Analysis, auth and audit tables |
| Media and job files | Snapshot/backup coverage for every client, system storage and batch queue; align recovery point with DB; include documents and outputs |
| Configuration and keys | Recoverable deployment configuration, credential-encryption keys and secrets through approved secret storage; separate access controls |
| Remote deliveries | Partner receipt/reconciliation evidence to avoid replaying already delivered files after restoring older local state |
| Application release | Source revision, dependency lock/version inventory, migrations and both service definitions |

Restore into isolation with workers/schedules initially stopped, no production
SFTP credentials and suppressed external email. Restore matching DB/media/config,
apply only the migrations appropriate to the selected release, verify tenant
isolation and source/output counts/hashes, then exercise one synthetic flow per
adapter. Inspect queued/running jobs and reconcile remote deliveries before any
worker is permitted to resume. Measure actual elapsed restore time and data-loss
window against approved RTO/RPO. Keep a dated signed result and clean up the test
environment through the operator's normal process.

An archive ZIP download or a second copy on the same disk is not evidence that
this procedure succeeds. AWS backup existence and restoration remain unverified.

## Deployment and rollback for this patch

No schema migration or frontend change is required by this patch. Deploy through
the existing reviewed release process; run `check`, then restart both services:

```bash
python manage.py check
sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

Restart the worker in a maintenance window after active work has drained: existing
startup recovery marks interrupted running jobs failed. Rollback is code-only to
the prior reviewed release; no data deletion or reverse migration is needed.
The observer fix affects future discovery; it does not automatically re-ingest
old folders. The baseline command never calls the folder observer or SFTP.
