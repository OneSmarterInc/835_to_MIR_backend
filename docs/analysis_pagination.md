# Analysis comparison pagination

The Analysis screen reads its isolated database projections. It no longer walks
the saved-file history or downloads all claims before rendering.

## Read contract

All reads use the existing authenticated, client-scoped
`/edi835/api/client-portal/analysis/` endpoint. Responses are private/no-store.

- `GET ?view=comparison&category=auto&page=1&page_size=10&sort=claim&direction=asc&search=`
  returns summary rows, filtered category counts, file counts and page metadata.
  `category` accepts `auto`, `both` (mismatches/unresolved), or `837-only`.
  Page sizes are 10, 25 or 50; unknown sorting/filter values return 400.
- `GET ?view=claim&claim=...` returns only the selected claim's source occurrences.
- `GET ?view=export&category=both&search=...&sort=claim&direction=asc`
  streams every filtered group as CSV, regardless of the visible page.
- `GET ?view=rejections&upload=...` returns at most 20 rejection explanations.
- `GET ?view=rejection-export&upload=...` streams the complete rejection report.
- `POST ?compact=1` runs the existing ingestion pipeline but returns only upload
  metadata and accepted/rejected counts. Clinical records and rejection arrays
  are omitted. The legacy GET/POST response remains available for older clients.

Duplicate uploads return HTTP 409 without storing another file. The check is
client/type scoped: identical bytes from a previously accepted upload, or all
valid claims already present by full business fingerprint, are duplicates even
when filenames or envelope controls differ. Mixed new/duplicate files still
save the new claims and report duplicate claims. Same-name files with changed
claims remain separate records with the original filename. Failed database
writes roll back so a retry can save normally.

## Query behavior

The list uses two queries after authorization: file counts, then a common-table
expression that produces category counts and paginated groups in one SQL snapshot.
Grouping happens before search/category filtering and pagination. Source matches
are never lost because another occurrence was on a different page.

Claims are normalized with `UPPER(TRIM(claim_number))`. Financial occurrences
are deduplicated by claim, source kind, exact filename, normalized internal
reference and all three source amounts. Different filenames or conflicting
financial records remain ambiguous. This preserves the existing comparison
rules; it does not replace ingestion's full-business-claim duplicate check.

Signed billed and allowed comparisons use integer cents derived from PostgreSQL
NUMERIC columns. Null values remain unknown. Both-match groups are excluded from
the mismatch view. The browser receives only counts and single values for summary
cells; filenames and occurrence arrays are fetched on opening a claim.
The Internal claim column and CSV use only 835 CLP07 references. Submission
references remain in stored source records but are not presented as 835 internal
claim numbers, including for existing uploads. 837 detail tables omit the column.

Search uses bound, escaped LIKE parameters and matches claim, internal reference
or source filename without narrowing the occurrences inside a matched group.
Ordering uses an allowlist, nulls last and a claim-number tie-breaker. The new
`analysis_upload_number_idx` index supports tenant upload / normalized claim
lookups; existing client-upload and upload-claim indexes support the list join.

CSV uses a PostgreSQL server cursor and batches of 100 comparison groups. Each
batch fetches its source occurrences in one query. This requires a direct or
session-pooled PostgreSQL connection, as configured for server-side cursors;
transaction-pooling deployments need a dedicated compatible export connection.
CSV values are escaped and formula-prefixed values are neutralized.

The UI aborts obsolete queries, preserves current filters/page after uploads,
and refreshes one comparison response per completed batch. Run counts remain
independent for 835/837 and reset for a new batch or retry.

## Verification

Backend:

```sh
python -m django test edi835.test_analysis_queries edi835.test_client_portal_analysis --settings=project835.test_settings
```

Run this suite against an isolated PostgreSQL test database as well. The maximum
NUMERIC-precision test intentionally skips SQLite; SQLite cannot represent the
same extreme decimal amounts faithfully. No production credentials are required
for the local suite.

Frontend:

```sh
npm run test:client-analysis
npm run test:client-routing
npm run build
```

Synthetic local SQLite measurement (100 files, 20,000 accepted occurrences):
100 history requests / 2,428,472 payload bytes become one page request / 7,550
bytes. The new request took about 0.09 seconds locally. This is not a production
latency measurement; verify PostgreSQL execution plans/timing with representative
data before claiming an AWS response-time target.

## Deployment

Deploy the backend and migration `0057_analysis_comparison_index` before the new
frontend. The index migration does not rewrite or reprocess existing claims.
Normal index creation can briefly block writes on a large staging table; apply
the migration during a quiet upload period. No cache or external service is added.

```sh
python manage.py migrate --noinput
python manage.py check
```

Then restart the existing backend service and deploy the frontend. Rollback of
the frontend is compatible with the retained legacy response contract.
