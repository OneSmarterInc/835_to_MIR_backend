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
during implementation. CSV export is unchanged. The Analysis UI shows separate
Denial code and Denial reason columns only for Mismatch amount. Download Excel
includes those adjacent columns only on the Mismatch amount sheet; Matched amount
and Missing in 835 retain their existing columns. Codes and reasons use the same
order, with a dash for an unknown reason. Excel lookups are batched, not per claim.

Validation: frontend Analysis regression and production build passed. Isolated
SQLite checks exercised the actual migration forward, reverse and reapply;
idempotent seeding; all 407 stored descriptions and metadata; numeric and
alphanumeric lookup; unknown codes; and tenant isolation. The full backend suite
was not run because this workspace lacks its complete dependencies.
