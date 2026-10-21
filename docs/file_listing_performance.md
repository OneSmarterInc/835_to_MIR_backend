# File-list query improvements

File listing and history endpoints keep their existing response fields, tenant
scoping, pagination defaults, and download behavior. No schema migration is needed.

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

The only live configuration file added on 50.17.152.89 is:

`/etc/systemd/system/mir.service.d/workers.conf`

Its contents are tracked in `deploy/mir-workers.conf`. It preserves the application
entry point and explicitly starts two Gunicorn workers. The existing service file
and environment file remain unchanged. Applying it requires:

```sh
sudo install -m 644 deploy/mir-workers.conf /etc/systemd/system/mir.service.d/workers.conf
sudo systemctl daemon-reload
sudo systemctl restart mir
```

Verify an active service, one master plus two children, and an HTTP response through
the normal route. A restart can briefly interrupt requests. Roll back by removing
only this drop-in, reloading systemd, and restarting MIR; the existing unit then
returns to its original worker setting.

The backend code in this PR has not been deployed to AWS. Deploy it separately after
review; do not overwrite the older AWS checkout with unrelated changes from main.

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
