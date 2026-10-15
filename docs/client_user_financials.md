# Client User financial amounts

The overview API (`/edi835/api/client-portal/overview/`) returns three independent
USD amounts for the authenticated Client User's assigned client.

| Label | Response field | Stored amount | Period event |
| --- | --- | --- | --- |
| Billed in 835 | `billed_835_amount` | `EDI835Claim.total_charge_amount` | `EDI835File.uploaded_at` |
| Billed MIR | `billed_mir_amount` | `MIRServiceLine.charge_amount` | `MIRFile.converted_at` |
| Amount in Recon | `recon_amount` | `RECONClaim.paid_amount` | `RECONFile.uploaded_at` |

## Source evidence and counting rules

- `edi835_claim_service.normalized_835_claims` stores CLP03 as the 835 claim
  charge. Count each normalized claim once, without adding service charges.
  Exclude synthetic `HELD_RELEASE` 835 files: these are internal release batches,
  not new inbound bills.
- `mir_persistence.store_mir_file` stores canonical MIR service positions 51–61
  within each 303-character service block as `charge_amount`. Sum generated
  services across continuation chunks once. Do not substitute source 835 bills,
  paid amounts, or the reconciliation waterfall. Include generated outputs
  regardless of SFTP delivery status; this is billing, not delivery confirmation.
- `recon_service` maps `amountinrecon` / `reconamount` to `paid_amount`, rolls
  service rows up into each `RECONClaim`, and stores file rollups separately.
  `reconciliation_service` and `recon_views._cash_summary` likewise use Recon
  paid amounts. Sum claim paid amounts only, without adding service/file rollups
  or fee fields. Include usable `PROCESSED` and legacy `PARTIAL` Recon files;
  exclude failed/in-progress files and `file_kind="837"` reference files.

Amounts are aggregated in three database queries. There is no cross-stream join
or claim-number matching, so unmatched Recon records are included and repeated
claim identifiers cannot multiply another stream's totals. Equal-valued rows
remain independent; `SUM(DISTINCT amount)` would incorrectly discard them.
Both parent and denormalized tenant ownership are checked where present.

Reprocessing replaces normalized rows/current MIR output; it does not add a
second version to the total. Distinct inbound file records remain independent
events, consistent with the ingestion code. In particular, identical hashes or
claim IDs do not establish that two separately received files are duplicates.
The dashboard reports current stored records, not an immutable financial ledger
or a historical snapshot of superseded conversions.

## Periods and display

All three streams use `resolve_period` in the client's configured IANA timezone
(existing fallback: `America/New_York`). Daily defaults to local today, Weekly
runs Sunday–Saturday, Monthly covers the full calendar month, All is unbounded,
and Custom includes both dates. Database bounds are local start midnight
inclusive and midnight after the end date exclusive, converted by Django for
storage comparisons. This includes full DST days without assuming 24 hours.

The API returns decimal strings plus `billed_835_claims`, `billed_mir_services`
and `recon_claims`. Zero records render as an em dash with a per-stream no-data
message; a recorded zero renders as `$0.00`. Missing fields render as unavailable,
never as a substituted total. Existing `billed_amount`, `paid_amount`,
`patient_amount` and `normalized_claims` fields remain for API compatibility.

## Verification

With test dependencies installed, a test-only `SECRET_KEY` set in the environment,
and `USE_LOCAL_SQLITE=True`, run from the backend repository:

```sh
python -m django test edi835.test_client_portal_financials \
  edi835.test_client_portal_period edi835.test_client_user_portal \
  edi835.test_client_attention_period_sort edi835.test_client_portal_claim_queue \
  edi835.test_client_portal_actions edi835.test_client_held_pagination \
  edi835.test_edi835_claim_service edi835.test_conversion_download_summary \
  --settings=project835.test_settings --noinput
```

Use `python -m django` for the isolated suite: the current `manage.py` loads
production settings through its database guard before applying `--settings`.
SQLite tests verify behavior, but do not validate PostgreSQL migration execution
or production query plans. No schema changes are required for these amounts.
