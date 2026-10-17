# Analysis staging

Analysis uploads are persisted separately from existing ingestion data. Migration
`edi835.0054_analysis_staging` creates `client_analysis_upload` and
`client_analysis_claim`. Apply it with the backend's configured production
environment before serving the updated Analysis page:

```sh
python manage.py migrate --noinput
```

This change does not backfill, merge, delete, or alter existing 835/837 records.
Analysis reads only its staging tables, scoped to the signed-in user's client.
New uploads accumulate and survive navigation/reload. No automatic expiry is
configured: these are durable staging tables, not connection-local SQL temporary
tables. They retain source bytes, filename, SHA-256, uploader, timestamp, and
claim sequence for a future explicit merge. Treat their contents and database
backups with the same access controls and retention policy as other claim data.

Validation precedes an atomic write of the source file and all normalized claim
occurrences. Repeating the same client, source type, exact filename and SHA-256
reuses the existing upload; a changed file is retained as a separate version.
Failed writes roll back both tables. The UI resets its selected file only after
a saved response, and offers retry when validation or saving fails. A lost
success response can safely be retried without duplicating stored data.

`GET /edi835/api/client-portal/analysis/` returns one upload's normalized claims
per page (`after` / `next_cursor`), never raw source bytes. `?csrf_only=1` returns
only the CSRF token. `POST` accepts one multipart `file` and `kind` (837 or 835).
The limits remain 10 MB and 20,000 claim occurrences per upload. As upload
history grows, server-side comparison/pagination can replace client aggregation
without changing the staging provenance.

The Analysis table groups by claim number and lists exact filenames once per
source type. Identical financial occurrences in the same filename are collapsed
for display; conflicting amounts remain visible for review, without summing or
arbitrarily pairing them. Source records remain intact in staging.

Comparisons use exact signed decimal amounts: CLM02 versus CLP03, and CLM02
versus the requested allowed amount, CLP04 + CLP05. Missing CLP05 means unknown,
not zero. Both match and mismatch rows remain visible in Analysis. This does
not change the separate persisted 835/837 status page's filtering rules.
