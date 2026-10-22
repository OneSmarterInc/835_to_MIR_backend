# Analysis: claim-level allowed reconciliation

This update supersedes the separate internal-claim rows in
`analysis_claim_state.md` and the previous nine comparison-column Excel layout.

## Main table and category rules

One row represents a normalized submitted claim number within one client.
All distinct normalized 835 CLP07 internal numbers appear on separate lines in
that row. Different submitted claim numbers and clients remain separate.
The 835 billed and Billed comparison columns are removed from the main table.

Payments, patient responsibility and allowed amounts combine distinct accepted
835 records for the claim. Existing deduplication, signed amounts and unknown
amount handling are preserved. A match means 837 billed equals either total
payments or total payments plus patient responsibility. CLP03 does not determine
the category. Missing 835 records go to Missing in 835; all other nonmatches,
ambiguous submissions, missing amounts and unresolved reversals remain in
Mismatch amount for review. A final reversal in any internal-claim sequence
prevents an automatic match until corrected, even if another internal claim is
more recent.

The list, counts, pagination, search and workbook use the same claim-level
grouping. Searching one internal number or filename returns the entire claim
and its totals. Source filename and internal-number lists are aggregated in SQL
without fetching raw file contents or adding per-row queries.

## Detail page

The overview displays overall billed charges, payments, responsibility, allowed
amount and allowed comparison. Each full internal ID gets an expandable section
with its own totals and source files. Files are identified by upload ID, so
identical filenames from separate uploads are not incorrectly collapsed.
Source rows display CLP sequence, payment date, CLP02, actual CLP03, payment,
responsibility and allowed amount. Latest source CLP03 follows upload order and
then CLP sequence, never an inferred sum or maximum. It is informational and
does not classify the claim. Original 837 source records appear separately.
Existing internal-ID deep links continue to open the whole claim.

## Excel contract

The supplied Claims-Reconciliation.xlsx provides the column layout. The export
adds Internal claim number immediately after Claim ID:

1. Claim ID
2. Internal claim number
3. 837 source
4. Billed ($)
5. Paid in supplied 835s ($)
6. Unconfirmed / difference ($)
7. Remit records
8. 835 sources
9. Notes

Difference is signed billed minus total paid. For missing remittance, the billed
amount is unconfirmed. This column is not the allowed-comparison delta: a claim
can be matched through patient responsibility while retaining an unpaid balance.
Notes explain that distinction. Unknown monetary values remain unavailable.
Remit records counts distinct accepted source CLP records after existing deduplication.
Source filenames and distinct internal numbers use separate lines in one cell.
The three sheet names stay Mismatch amount, Matched amount and Missing in 835.
Logo, FileX wordmark, Filex OS metadata, grid borders and numeric currency cells
are retained. IDs and filenames remain literal text to avoid formula execution.

No migration is introduced. Deploy both frontend and backend for aligned views.
Validation uses synthetic fixtures; production and PostgreSQL execution are not
certified by the local SQLite test run.
