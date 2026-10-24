# Analysis CAS reason lookup

Migration `0070_claim_adjustment_reasons` creates `claim_adjustment_reason` and
seeds all 407 unique codes from the supplied code list. Descriptions are preserved
verbatim; start/modified/stop dates and notes are stored separately as source
metadata. Stopped codes remain available for historical remittances.

Analysis returns the existing `denial_codes` plus `denial_reasons` containing
`code` and nullable `reason`. The CAS group stays in the displayed code (for
example `PR-1`), while the lookup uses the reason component (`1`). Alphanumeric
codes such as `B11` are supported. Unknown codes remain visible without invented
descriptions. Lookups are batched for the current page and use the existing
client-scoped claim extraction. Saved normalized uploads need no reprocessing.

Deploy the backend migration before serving the updated backend code:

```sh
python manage.py migrate --noinput
```

Then restart the backend and deploy the frontend. Pushing Git commits alone does
not populate a running deployment's database. No production database was changed
during implementation. CSV export is unchanged.

Migration `0071_remittance_code_crosswalk` adds all 124 rows from the supplied
`CAS_CARC_RARC_Crosswalk.xlsx`, including 46 distinct CARCs and 47 distinct RARCs.
It preserves original descriptions, repeated mappings, and source metadata.
Missing RARC markers are stored as empty codes, with their original values in
metadata. Conflicting description variants are all shown as source descriptions;
no arbitrary first-match description replaces them. The older 407-code reference
remains the fallback for CARCs absent from the crosswalk.

The mismatch table shows a separate CAS column (for example `CO-288`), followed
by CARC and RARC code columns, with descriptions on hover
and accessible labels for keyboard focus. Actual RARCs are read from LQ02 with
HE, MIA05/20/21/22/23, and MOA03 through MOA07, never inferred from the workbook's
reference pairings. See the [Indiana Medicaid 835 guide](https://www.in.gov/medicaid/providers/files/modules/financial-transactions-and-remittance-advice.pdf).
Unknown codes remain visible with an unavailable description. Old API denial
fields remain for compatibility; new `carc_codes` and `rarc_codes` arrays contain
code/description objects.

Excel adds CAS, CARC code, CARC description, RARC code and RARC description only to
Mismatch amount. Matched amount and Missing in 835 retain their existing columns.
Descriptions follow code order with a dash for unknown descriptions. Lookups are
batched per page/export batch, not per claim. Apply both migrations before serving
the updated backend. Existing normalized uploads do not need reprocessing.

Validation: frontend Analysis regression and production build passed. Isolated
SQLite checks exercised the actual migration forward, reverse and reapply;
idempotent seeding; all 407 stored descriptions and metadata; numeric and
alphanumeric lookup; unknown codes; and tenant isolation. The full backend suite
was not run because this workspace lacks its complete dependencies.
