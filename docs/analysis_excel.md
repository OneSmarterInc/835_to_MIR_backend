# Analysis Excel export

The Analysis download button now requests `view=excel` and downloads
`Claims-Reconciliation.xlsx`. The previous CSV endpoint remains available for
existing callers and rejection downloads remain CSV.

The workbook has three sheets in this order: Mismatch amount, Matched amount,
Missing in 835. All sheets use the nine comparison-table columns and contain all
matching pages, not just the currently selected category or page. The current
search and ordering apply to the whole workbook. Empty categories retain their
headers. No colored fills, status colors, charts or source-file columns are added.

The existing database comparison query supplies every category from one result
set, preserving tenant scoping and the CLP selection and per-internal-claim totals described
in [Claim state selection](analysis_claim_state.md).
Identifiers are literal text, including leading zeroes and formula-like prefixes.
Amounts are numeric currency cells; unavailable values remain an em dash. The two
comparison cells contain their status and known difference, matching the screen.

The exporter uses the existing openpyxl dependency, now explicitly pinned to
3.1.5 in req.txt. Write-only worksheets and batches of 500 records avoid building
all spreadsheet cells in memory. The response file spills to temporary disk above
8 MiB and is closed by FileResponse. The query still performs the full comparison
for the requested search; exporting a large history takes longer than one table
page. A sheet exceeding Excel's row limit returns a search-narrowing error.

Verification: 23 Analysis query and Demo-access tests ran, with 22 passing and
one PostgreSQL-only test skipped. Frontend Analysis tests, targeted lint and the
production build passed. Existing unrelated frontend build warnings remain.
All three synthetic export sheets were reopened and rendered for visual review.
Tests cover sheet order, headings, numeric values, signed totals, missing values,
formula-safe IDs, leading zeroes, tenant isolation, full pagination, sorting,
search, empty sheets and download availability on an empty selected tab.

The subsequent reconciliation update requires migration 0063, openpyxl 3.1.5
and Pillow 12.3.0. Each sheet has a transparent red-and-silver mark followed by
the FileX wordmark, a category title, then plain headers in row 3. Data starts
at row 4, with C4 frozen. Author and last editor metadata are Filex OS.
Thin cell borders form a visible grid around the table. Worksheet gridlines
outside the table remain hidden; data has no colored fills. The header uses
the supplied OSI CUT logo with a transparent background.
Production and PostgreSQL execution have not been performed locally.
