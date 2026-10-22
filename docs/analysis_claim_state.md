# Analysis claim grouping, CLP state and Excel grid

## Current business rule

Group by client, submitted claim/CLP01 and the full normalized 835 internal
claim number (CLP07). Different internal IDs get separate comparison rows,
payment totals, source details and export rows. Equal masked suffixes never
establish identity. Missing internal IDs remain separate occurrences, and the
same internal ID under a different patient control number is not merged.

For each group, select an actual source CLP record using:

1. Most recent upload timestamp, as explicitly chosen by the user; upload ID
   breaks equal timestamps deterministically.
2. The last original CLP sequence within that uploaded file.
3. Inspect the selected entry's stored CLP02. A final reversal (22) is shown as
   Reversed — review and cannot enter Matched amount. A later correction in
   the same group supplies its own CLP03. No previous amount is silently
   substituted for a final reversal.

Use the selected record's CLP03 directly, including its original sign. Never
sum CLP03, reconstruct it from CLP04, choose the maximum amount, or treat an
increase as proof of chronology. This upload/sequence ordering is the
application's requested policy, not a claim that X12 defines upload time as
adjudication chronology. Source details show CLP02 and identify the entry used.
BPR16 remains visible as source information but no longer decides ordering.

For A=(CLP03 30, CLP04 30) and B=(CLP03 60, CLP04 30), B selected last gives
CLP03 60 and payment total 60. A later entry with CLP03 20 instead gives 20.
Payments and responsibility retain signed sums only within the same internal
claim group. Allowed comparison still accepts either payment total or payment
plus responsibility. Both comparisons must match for the Matched amount tab.

The source pair supplied during diagnosis contains different full CLP07 values;
those two entries therefore remain separate under the clarified requirement.
They must not be combined merely because their masked endings look the same.

## Implementation and compatibility

The existing normalized Analysis835Claim supplies status; AnalysisClaim supplies
original sequence and upload provenance. No new migration or source re-upload
is required by this change. Existing migration 0063 remains a prerequisite.
The database query performs grouping and selection before pagination and uses
full-claim fingerprints where available to distinguish records with identical
financial fields but different claim content. Existing ingestion duplicate
rejection remains unchanged.

Each row carries a composite key so detail navigation and reload select the
correct internal claim. A legacy claim-number-only link still works for a
single group; an ambiguous old link asks the user to select a specific row.
All keys remain tenant scoped and SQL parameters remain bound. CSV and Excel
use the same selected records, statuses and group boundaries as the screen.
Deploy backend and frontend together. No production deployment was performed.

CLP02=22 identifies a reversal; reversal/correction handling requires more than
choosing a large charge amount. Primary reference:
https://x12.org/resources/requests-for-interpretation/rfi-1429-cas-required-backed-out-cla

## Workbook

Each of the three sheets retains the FileX wordmark and Filex OS author
metadata. The supplied OSI CUT.png replaces the old mark; its dark background
was removed with the built-in image editor. The saved alpha-transparent asset
is edi835/assets/filex-mark.png, downscaled to 160 pixels for compact embedding.
Prompt: remove only the dark background, preserve the supplied S ribbon and
silver sphere, use transparent alpha, and clean stray edge pixels.

Thin neutral borders surround all nine header and data columns. Branding rows
remain borderless; no colored data fills are introduced. All three synthetic
sheets were reopened and rendered for inspection, including the new logo.

## Verification

Analysis query, upload and Demo-access tests: 72 run, 71 passed, one
PostgreSQL-only check skipped. See analysis_claim_state_verification.json.
Frontend Analysis and route/reload tests, targeted lint and production build
passed. Existing unrelated bundle warnings remain. Cases cover upload order,
CLP sequence, decreasing charges, reversal/correction, internal-ID separation,
missing IDs, tenant isolation, separate source details, exports and table grids.
PostgreSQL and live AWS execution remain unverified.
