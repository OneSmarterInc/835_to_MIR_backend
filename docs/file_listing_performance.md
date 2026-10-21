# File-list query improvements

File listing and history endpoints keep their existing response fields, tenant
scoping, pagination defaults, and download behavior. These query changes need no
schema migration. The later release compatibility repair includes accounts 0026;
run all pending migrations when deploying that repair.

- 837 lists defer stored file contents.
- Client overview/history defer source documents and unused raw claim data.
- The four-stream dashboard defers document contents and counts each stream once.
- Dashboard counters use one conditional aggregation, preserving the existing
  historical fallback when today's converted claim total is zero.
- Archive listing joins clients up front, skips conversion findings, and resolves
  the archive directory once per client rather than once per file.

Archive listing still checks file presence and reads filesystem metadata, preserving
its physical-archive contract. RECON findings and legacy row limits are unchanged.
Removing those operations or changing default pagination needs a coordinated API/UI
change. These changes do not claim a measured end-to-end latency improvement.

## AWS worker configuration

The optional template `deploy/mir-workers.conf` targets
`/etc/systemd/system/mir.service.d/workers.conf`. Live installation must be verified
on the server; a repository template is not evidence of deployed configuration.

The original template replaced the entire `ExecStart` command, losing any bind,
socket, timeout, logging or other options from the existing service. The corrected
template sets `GUNICORN_CMD_ARGS=--workers 2` and preserves `ExecStart`.

Inspect `sudo systemctl cat mir.service` first. If `GUNICORN_CMD_ARGS` already
contains options, merge `--workers 2` into that value. If `ExecStart` explicitly
sets workers, change only that option because command-line options take precedence.
Do not overwrite unknown deployment settings. After reconciling those values:

```sh
sudo install -m 644 deploy/mir-workers.conf /etc/systemd/system/mir.service.d/workers.conf
sudo systemctl daemon-reload
sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

Verify an active service, one master plus two children, and an HTTP response through
the normal route. Let active conversions finish before restarting the batch worker.
A restart can briefly interrupt requests. Keep a copy of the prior service settings;
roll back only the reviewed worker override, reload systemd, and restart both
`mir.service` and `mir-batch-worker.service`.

## Validation

Tests use synthetic records and an isolated in-memory SQLite test database, never
the production database. Query-capture checks ensure document content columns are
absent from list queries, responses retain expected fields, and tenant filtering is
preserved. Metrics tests verify one-query aggregation and historical fallback.

Executed suites:

- `edi835.test_listing_query_efficiency`
- `edi835.test_tracked_file_performance_api`
- `edi835.test_client_user_portal`
- `edi835.test_client_portal_financials`
- `edi835.test_client_portal_period`

The tests do not replace PostgreSQL query-plan verification or authenticated browser
performance measurements on the deployed version.
