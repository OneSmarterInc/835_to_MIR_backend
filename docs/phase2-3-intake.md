# Phase 2–3 intake implementation and release checks

## Delivered behavior

| Requirement | Implementation and boundary |
| --- | --- |
| Cross-channel file duplicates | Client + workflow + SHA-256 of source bytes is checked under the client row lock. A partial unique constraint protects canonical active receipts. Filename, actor and channel do not change identity. |
| Linked duplicate receipts | Each new duplicate submission has its own immutable source receipt, `canonical_transfer_id`, completed dispatch and `DUPLICATE` lifecycle state. It starts no processing attempt. Repeating the same Idempotency-Key returns the same receipt. |
| Corrections and reversals | Changed bytes, including financial amounts, dates or reversal segments, are new inputs. File identity never uses claim ID alone. Analysis retains its independent claim-content fingerprint rules. |
| MIR provenance | `IntakeTransactionSet` records receipt, ST/SE position, type, control number, version and normalized transaction digest. Outputs link to source transaction sets from accepted inputs; rejected batch sources are excluded. Combined MIR outputs can have several source receipts. |
| Indexes and timestamps | Receipt history has client/date and state/age indexes; duplicate lookup has client/workflow/checksum indexing. Existing dispatch and lease indexes remain. Dates use timezone-aware Django timestamps. |
| Concurrency and recovery | Synthetic tests cover duplicate submissions across channels, mixed batches, database constraints, idempotency and fenced output replay. A separate PostgreSQL test races three channels for one canonical queued job. Existing dispatch/lease/crash tests remain in the regression suite. |
| Staff client selection and guidance | Existing permitted-client selection remains. Conversion controls prevent changing client during an upload/processing operation. File guidance reflects the existing 835 extension policy; Analysis continues accepting extensionless X12. |
| Upload vs processing progress | Browser upload byte progress is separate from validation/processing. Analysis and Conversions expose receipt status links. Staff can expand a paginated, client-scoped history covering portal, SFTP and API channels. Details are fetched only on demand. |
| Sessions, MFA, CSRF and tenant scope | Existing authentication controls remain. XHR uses authenticated credentials and the server-issued CSRF token. Demo receipt access is restricted to its assigned client's Analysis workflows. Worker authorization is rechecked for manual conversion. `intake_readiness` checks effective production settings. |
| Upload limits and content | Common intake enforces per-file bytes, aggregate request bytes and file count. Declared oversized HTTP bodies receive 413 before parsing. Django bounds JSON body memory. Archive/executable signatures and extensions are rejected even when renamed. An nginx fragment supplies the corresponding gateway limit. |

## Important distinctions

- The file duplicate policy compares **exact bytes**. Changing only an envelope control number or whitespace creates a different file identity. Claim-level duplicate handling remains separate; a financial correction or reversal is never discarded merely because its claim number was seen before.
- Analysis remains isolated through `ANALYSIS_835` / `ANALYSIS_837` workflows. A fully rolled-back Analysis save can be retried if no upload with that checksum was saved. A failed MIR run is not automatically converted again: output or external delivery may already have occurred. Use receipt history and the existing recovery procedure.
- Read-only `VALIDATE_835` operations may run again to produce a fresh validation report. They are not logical conversions. Legacy unassigned system operations remain outside client-scoped deduplication.
- Manual conversion of an already validated file now registers its source before acknowledging the queue operation. Where the validation receipt exists, the original uploaded bytes—not parser-normalized text—determine identity. Source integrity is checked before commit.
- A duplicate SFTP source is retained for operator disposition; it is not resent or automatically deleted. A subsequent poll may create another audit receipt. Remote cleanup must follow confirmation of the canonical processing/delivery result.
- Transaction lineage identifies converter inputs. It does not claim that every claim in a partially accepted transaction was emitted to MIR. Existing normalized MIR claims and held-claim findings remain the authoritative claim-level result. Legacy outputs are not assigned invented transaction provenance.
- The duplicate audit keeps a private source object per receipt. This favors independent auditability over physical storage deduplication. Rejected pre-registration objects are handled by the existing orphan reconciliation process.

## Migration and deployment

Migration `0064_intake_dedup_provenance` adds the relations, indexes and constraint. Historical identical receipts are linked to a canonical completed receipt where possible; their original outcomes and outputs are preserved. The migration deliberately stops if a historical duplicate group still has queued/running dispatches. Drain or review those jobs first; do not delete them to bypass the guard.

1. Back up PostgreSQL and private source/output storage. Close portal/partner intake and pause SFTP scheduling; let in-flight work finish. Record the current backend revision and schema.
2. Stop both backend producers and the worker for the migration maintenance window.
3. Pull the backend release and run migrations, configuration checks and the read-only receipt audit.

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
sudo systemctl stop mir.service mir-batch-worker.service
git pull --ff-only origin main
python manage.py migrate --noinput
python manage.py check
python manage.py intake_readiness
python manage.py intake_audit
```

Review `deploy/intake-nginx.conf.example` against the **existing** API server configuration. Default application limits are 100 MiB/file, 128 MiB/request, 100 files/request; Analysis remains 10 MiB/file. Keep gateway request size aligned with `INTAKE_MAX_REQUEST_BYTES`; do not replace existing TLS/proxy/CORS settings with the example fragment.

Only after migration and checks succeed:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

Deploy the frontend after the backend. The added response fields are backward compatible with the previous UI. Reopen intake only after smoke checks. If migration fails, keep intake closed and inspect the named blocker; do not continue to restart commands automatically. No migration deletes existing source or MIR records. Rolling back application code alone while keeping these schema additions is preferable to deleting audit records to reverse the migration.

## Verification

The local regression report is `phase2-3-intake-regression.json`. Tests use synthetic data and an isolated SQLite database; production data was not used. The PostgreSQL row-lock tests are deliberately skipped on SQLite and must pass on an isolated PostgreSQL test database before production release:

Verified on 22 October 2026: 387 backend tests ran, with no failures/errors and four PostgreSQL-only skips (383 passed). The isolated source fingerprint matches the delivered source. Frontend Analysis integration, four upload-transport tests, eleven CSRF/request-stack checks and the production build passed. Targeted lint reported existing unused-variable warnings; the build retained its existing large-bundle and MappingPreview CommonJS warnings.

```bash
# DATABASE_URL must point to a disposable staging database/user with CREATEDB.
# Never point the Django test command at production credentials.
python manage.py test edi835.test_intake_postgres --noinput
```

Other checks:

```bash
python manage.py makemigrations --check --dry-run
python scripts/run_phase1_checks.py --report docs/phase2-3-intake-regression.json
# Frontend repository:
node scripts/test-upload-request.js
npm run test:csrf
npm run test:client-analysis
npm run build
```

Production-only checks still required: real PostgreSQL race results, effective MFA/session enforcement, gateway 413 responses, authorized/unauthorized client smoke tests, real SFTP host trust/account restrictions, and one synthetic upload → receipt → worker → durable output → receipt-status trace. Local checks do not establish those deployment facts. The nginx example and readiness command do not modify AWS configuration automatically.
