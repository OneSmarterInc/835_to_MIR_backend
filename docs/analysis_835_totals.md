# Analysis: totals across 835 records

Historical implementation report. The CLP03 summation and allowed-match rules
below are superseded by [Latest payment reconciliation](analysis_latest_payment.md).

Analysis now displays one monetary total per claim when multiple 835 source
records exist. Previously, these records made the amounts and comparisons appear
as "Multiple" even when the combined remittances could be compared.

## Calculation and display

- 835 billed: sum of CLP03.
- Claim payment: sum of CLP04.
- Patient responsibility: sum of the stored responsibility values (CLP05 or the
  existing PR adjustment fallback).
- Allowed: sum of payment plus responsibility for each contributing record.
- Billed comparison: the single 837 CLM02 versus total 835 billed.
- Allowed comparison: the single 837 CLM02 versus total allowed.

Both comparisons must match for the Matched amount filter. Amounts retain their
signs, so reversals subtract from totals. Missing values make the affected total
unknown rather than treating missing money as zero. Multiple distinct 837
submissions still require review; the change does not choose a submission.

The existing record deduplication remains in place. Distinct source files with
equal amounts contribute separately; repeated identical projection records from
the same filename do not contribute twice. Internal claim identifiers are not
summed and can still display Multiple. Claim details preserve individual source
records. No historical data is modified.

## Implementation

The database comparison query calculates the totals before filtering, sorting,
counting and pagination. Searching for one contributing filename still compares
the complete claim group. List, detail and CSV endpoints use the same totals.
The frontend comparison utility follows the same calculation using integer cents;
column subtitles identify the 835 fields as totals.

No schema migration or re-upload is required for this calculation change. Deploy
the backend and frontend together. This does not remove the migration and host
trust prerequisites of the preceding SFTP changes; see `phase2_sftp.md`.

## Verification

- Backend regression suite: 362 tests, 359 passed, three PostgreSQL-only checks
  skipped on local SQLite. See `analysis_835_totals_verification.json`.
- New regression cases: summed billed and allowed matches, category counts,
  filename search retaining all contributions, source detail, CSV totals,
  signed reversals, unknown responsibility, repeated records, and equal amounts
  in different files.
- Frontend: `npm run test:client-analysis`, production build, and targeted Oxlint
  passed. The build still reports unrelated existing MappingPreview CommonJS and
  large bundle warnings.
- No live production or PostgreSQL execution was performed for this change.

Verification commands:

```text
python scripts/run_phase1_checks.py --temp-dir <short-local-temp-directory>
npm run test:client-analysis
npm run build
```

Tradeoff: totals intentionally combine all retained 835 records in a claim group,
including separate remittance events. Source detail remains the audit trail for
understanding each contribution; this change does not infer which remittance
supersedes another.
