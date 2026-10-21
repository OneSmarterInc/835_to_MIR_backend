# Phase 1: compatibility and partner routing matrix

Repository evidence as of 21 October 2026. “Covered by fixture” means a tested
behavior, not complete conformance to an implementation guide or partner approval.
No new version rejection policy is enabled by this documentation.

## Format and validation matrix

| Input / route | Current evidence | Coverage / limitation | Release status |
|---|---|---|---|
| 835 manual validation | `converter/views.py:_validate_835_for_conversion`, `converter/services/validator.py`, `validation.py`; fixture in `converter/tests.py` declares ISA12 `00501`, GS08 `005010X221A1` | PyX12 plus structural checks and a narrow CLP01 warning exception; installed validator maps and partner-specific rules need verification | Fixture-backed baseline; partner certification pending |
| 835 main SFTP / conversion | `edi835/services.py`, `full_sftp_pipeline.py`, `sftp_automation_operations.py` | Batch input calls validation; single processing calls shared parser/mapper. Not all routes use the same validation wrapper | Route-parity work remains before claiming one universal acceptance gate |
| 837 main portal | `edi835/edi837_service.py:parse_837`, `edi835/test_edi837_service.py` uses `005010X222A1` | Extracts supported claim/service fields and requires relevant envelope/type tags; not a complete guide/version whitelist | Tested professional-style fixture; no blanket 837 version approval |
| Analysis 835 | `client_portal_analysis.py:parse_analysis`; corrected test envelope uses `005010X221A1` | Envelope/count/type checks and per-claim amounts; shared normalized mappings. Explicitly not payer-guide validation | Tested structural and comparison behavior |
| Analysis 837 | Same parser; test envelope uses `005010X222A1` | Shared main 837 mappings after stricter structural/claim validation; version identifier alone is not enforced as an allowlist | Tested structural and comparison behavior |
| Other 835/837 guides, older versions, institutional/dental variants | No complete partner acceptance corpus established by this review | Parser accepting a file does not establish coverage of all fields, balancing rules or guide requirements | Unverified; obtain approved fixtures before promising support |
| RECON and MIR | `recon_service.py`, MIR mapper/exporter/persistence | Partner-specific layouts and mapping configuration, not interchangeable with 835/837 validation | Verify against partner expected-output fixtures |

The old Analysis fixture incorrectly labelled its 835 GS envelope with the 837
guide identifier. That fixture is corrected. Runtime parsing was not tightened
silently: imposing an unapproved version allowlist could reject existing traffic.

## Filename, amount and duplicate conventions

| Convention | Current behavior and source |
|---|---|
| Extensions | `edi835/file_types.py`: main 835 allows `.835`, `.x12`, `.edi`, `.txt`, `.dat`, `.35`, `.ansi`, `.rem`; main 837 allows `.837`, `.x12`, `.edi`, `.txt`, `.dat` and extensionless names. Analysis validates uploaded content rather than relying on extension |
| Source/display versus storage name | Preserve original filename metadata; physical collision handling may choose a distinct stored name within a client directory. Same name is not proof of duplicate content |
| Analysis limits | 10 MiB/file, 20,000 claims, 200,000 segments in `client_portal_analysis.py`. Main 837 has configurable `EDI837_MAX_UPLOAD_BYTES` with 100 MiB default; these are route-specific limits, not measured safe capacity |
| Main 837 identifier mapping | `CLM01` plus partner-specific `REF*9C` / combined identifier handling in `edi837_service.py`; needs partner confirmation |
| Analysis internal claim | Comparison reads the 835 internal identifier (`CLP07`); do not infer an Analysis internal ID from an unrelated 837 field |
| Analysis money | Signed decimal cents; compares CLM02 to CLP03 and to CLP04 + patient responsibility; explicit CLP05 wins, otherwise CAS PR fallback or zero where absent |
| Exact match | Both billed and allowed comparisons must match for the matched category; unresolved/multiple occurrences need review rather than inventing an amount |
| Duplicate policy | Analysis uses client/type-scoped raw digest and normalized business-claim identities. Whole duplicate uploads return 409 without adding an accepted source; partial duplicates produce rejected claims. Main 837 intentionally stores repeated arrivals; no claim of universal cross-route deduplication |
| Client routing | Authenticated/selected client plus explicit admin SFTP-purpose configuration. No verified universal mixed-client-content detector or partner sender mapping |

## Fixture inventory and missing cases

All listed repository fixtures are synthetic test evidence. Do not copy real
patient data into this documentation or a public test corpus.

| Existing evidence | Behavior exercised | Additional evidence required |
|---|---|---|
| `edi835/test_client_portal_analysis.py` | Envelope/count rejection, multiple transactions/interchanges, alternate delimiters, partial claims, signed amounts, blank CLP05 fallback, duplicates, isolation and schema parity | Real partner guide compliance; PostgreSQL concurrent duplicate uploads |
| `edi835/test_analysis_queries.py` | Server-side grouping/comparison/pagination behavior | PostgreSQL precision-specific test and representative-volume query plans |
| `edi835/test_edi837_service.py` | Normalized claims/services, subscriber/dependent context, claim slice, client scoping, repeated arrivals | Guide-specific unsupported loops and partner acceptance outputs |
| `edi835/test_full_sftp_combined_835.py` | Combined MIR and delete-after-delivery behavior with mocked remote operations | Isolated SFTP sandbox failure/replay tests with actual server semantics |
| `edi835/test_storage.py` | Client directory layout, collision-safe archives, delivery cleanup | Filesystem-full, permissions, crash and restore scenarios |
| `project835/test_release_requests.py`, `tests_csrf.py`, `test_demo_access.py` | Session/CSRF/role contracts for important request paths | Deployed cookie/domain/proxy and production role smoke tests |
| `edi835/test_phase1_baseline.py` | Independent same-name client discovery, metadata-only aggregates and temporal boundaries | Read-only PostgreSQL run and infrastructure measurements |

## Partner sign-off worksheet

Populate one row per client, partner and transaction/guide combination in a
restricted operations record. Values are deliberately not invented from filenames.

| Required field | Evidence to collect | Owner |
|---|---|---|
| Client and partner identity | Assigned client UUID, approved sender/receiver identifiers, mixed-client rule | Integration lead |
| Transaction / guide | ISA12, GS08, applicable ST03; partner companion-guide revision | Integration lead + QA |
| Transport / trust | Route purpose, source/target folders, server host-key verification, credential rotation owner; references only, never passwords | Infrastructure + security |
| Filename / readiness | Original name convention, temporary-name/finalize protocol, collision and archive behavior | Integration lead |
| Claim identity / amounts | CLM01, CLP01, CLP07/REF conventions, reversals, adjustments, precision and expected MIR fields | Integration lead + product |
| Acceptance corpus | De-identified source, expected normalized rows/MIR, expected rejection cases, hashes and test outcome | QA |
| Approval | Named approver, date, supported routes and unresolved exceptions | Technical lead + partner |

Until this record is populated and approved, classify compatibility as
“documented/tested for listed fixtures; partner verification pending”.
