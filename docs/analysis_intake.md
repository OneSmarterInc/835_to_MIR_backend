# Analysis claim intake

Analysis uses isolated `client_analysis_*` tables. It never ingests into the live 835/837 tables or sends files to SFTP/MIR delivery.

## Processing

1. Validate every ISA/IEA, GS/GE and ST/SE envelope and count, including concatenated interchanges with separate delimiters.
2. Validate claims independently, retaining original occurrence numbers. Accept leading-decimal monetary forms such as `.50` and `-.50`; reject malformed money without silently replacing it with zero.
3. Use the main 837 parser and column mappings, and the main 835 normalizer. Store 837 patient/subscriber/provider/diagnosis/date/claim fields and related service rows in `client_analysis_837_claim` and `client_analysis_837_service_line`. Store 835 status, monetary amounts, service count, raw claim and segment context in `client_analysis_835_claim`. The existing comparison index remains for API compatibility. Field types match their main intake counterparts and are covered by a schema parity test.
4. When CLP05 is absent, sum claim- and service-level CAS PR adjustments. With no PR adjustments, use zero. Explicit CLP05 is retained without adding adjustments again. Allowed amount remains payment plus patient responsibility. Source: https://x12.org/resources/requests-for-interpretation/rfi-1569-situational-cond-truth-source
5. Reject exact duplicates within a file and across saved Analysis files of the same client/type. Identity includes the full business segments plus normalized claim/service columns (including dates, people, providers, amounts and adjustments). Filename, upload time, envelope control numbers and hierarchy ordinals are excluded. Changed business content is retained. A tenant lock and database unique constraint protect concurrent intake.
6. Atomically save accepted comparison rows, normalized claims/services, duplicate identities, the source, and rejection diagnostics. Files with no valid claims retain the source/diagnostics with REJECTED status and no comparison rows. Unsafe envelope errors do not save anything. An identical already-accepted file retry is rejected with HTTP 409 without adding rows.

The UI shows accepted/rejected claim counts in each upload box. Rejections survive reload and can be downloaded as a report. Successful partial saves reset the picker; transport/database failures retain files for retry.

## Deployment and existing uploads

Deploy both backend and frontend. Run migrations before restarting the new backend. Reprocess old Analysis sources once to populate normalized tables, recover omitted responsibility, and remove exact duplicates from the comparison index. Raw source bytes remain unchanged. The command processes files in upload order, one source at a time; it does not read the live claim tables.

```bash
cd /var/www/835_to_MIR_backend &&
git pull --ff-only origin main &&
source venv/bin/activate &&
python manage.py migrate --noinput &&
python manage.py reprocess_analysis_uploads --apply &&
python manage.py check &&
sudo systemctl restart mir.service mir-batch-worker.service &&
sudo systemctl status mir.service mir-batch-worker.service --no-pager
```

Use `python manage.py reprocess_analysis_uploads` for a validation-only dry run. `--client-id UUID` limits scope. Applied runs also perform duplicate detection; subsequent runs skip already processed sources. Structural failures leave that legacy source unchanged and report its upload ID; investigate before rerunning. Files rejected before being saved must be uploaded again.

## Verification

```bash
python -m django test edi835.test_client_portal_analysis edi835.test_edi835_claim_service project835.test_client_user_conversion_access --settings=project835.test_settings --noinput
npm run test:client-analysis
npm run test:client-routing
npm run build
```

Run Python tests with a test-only SECRET_KEY, DATABASE_URL=sqlite:///:memory: and ALLOW_SQLITE_FOR_TESTS=1, and npm commands from the frontend repository. SQLite verifies behavior but not PostgreSQL locking. Validate concurrent intake and migrations in PostgreSQL staging before production rollout.
