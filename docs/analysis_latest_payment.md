# Analysis: latest billed amount and cumulative payments

835 billed is now CLP03 from the latest BPR16 payment date inside the file.
Upload order and filename dates never decide the billed amount. CLP04 payments
and patient responsibility continue to accumulate across distinct records,
including signed reversals. The allowed column remains their combined total.
Allowed comparison matches when either payment total or payment plus
responsibility equals the 837 CLM02. Payment-only matches explicitly say
"Total payments match" in the screen and Excel export. Both comparisons must
match for the Matched amount category.

Missing dates with conflicting billed values, or conflicting values on the
latest date, produce "Latest 835 needs review". When all billed values agree,
that consensus is usable without selecting a date. This avoids inventing an
ordering for ambiguous remittances. Full internal IDs are normalized for case,
outer whitespace and the existing ZZ: prefix; identical IDs display once.
Different IDs are never merged merely because their masked endings match.

## Storage and deployment

Migration 0063 adds AnalysisClaim.payment_date and backfills existing projection
rows from the retained raw 835 using transaction boundaries and original claim
sequence. It also normalizes existing 835 internal IDs. It processes one upload
at a time, without reuploading or reinserting claims or changing source amounts.
Invalid or absent dates remain unknown. Source details expose BPR16 separately
from upload time. Deploy backend and frontend together, install req.txt, run
python manage.py migrate, and restart BOTH mir.service and
mir-batch-worker.service. The backfill duration depends on retained history.
No production migration or deployment was executed locally.

The user selected payment date as the ordering rule. X12 identifies BPR16 as the
payment date: https://x12.org/resources/requests-for-interpretation/rfi-2061-adjudication-or-payment-date

## Excel presentation

The button has an Excel icon and exports three sheets with the same comparisons,
search and ordering as the table, independent of the selected page/category.
Each sheet starts with the existing red-and-silver mark and FileX wordmark at
top left, followed by its category and the nine table columns. The logo is an
alpha-transparent PNG with no baked-in checkerboard; native text remains crisp.
Gridlines are hidden and data has no colored fills. The header occupies three
rows; filters start at row 3 and panes freeze at C4.
Author and last editor are Filex OS; title, subject, description, category and
keywords also identify FileX. Pillow supports image embedding.

The mark was cleaned with the built-in image editor using the existing portal
favicon as the edit target, preserving the red-and-silver design. Prompt:
"Remove all baked-in checkerboard background, including inside openings; use
transparent alpha; preserve the S silhouette, colors and metallic detail;
no redesign, extra text, or outside shadow." The result is downscaled to 160px
in edi835/assets/filex-mark.png for compact workbook embedding.

## Verification

The full synthetic backend suite passed 365 tests with three PostgreSQL-only
checks skipped; see analysis_latest_payment_verification.json. Frontend Analysis
tests, targeted lint and production build passed. Existing unrelated build
warnings remain. Regressions cover latest-date selection despite upload order,
missing dates, same-day conflicts, payment-only matches, canonical full IDs,
migration upgrade/repeat, tenant isolation, Excel metadata and transparent
logos on all three sheets. All three synthetic worksheets were rendered for
visual inspection. PostgreSQL and live deployment remain unverified.
