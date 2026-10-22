# Intake Control — implementation and rollout

Implemented locally on 2026-10-22. This document describes application behavior, not evidence of an AWS deployment or production acceptance.

## Access the screen

After deploying both repositories, sign in to the administrator portal and choose **Intake Control** in the sidebar. Direct route: `/administrator?nav=intake-control`. The five sections also persist in `intakeTab=operations|partners|quarantine|retention|access`.

Superadmins see the screen automatically. For an ordinary administrator, a superadmin must enable **Intake Control** in **Access → Edit profile → Screens**. Existing administrators are not silently granted partner-management authority. The existing Operations screen now shows the same live operational data in read-only mode; its fabricated headline figures have been removed.

The backend is `/admin-panel/api/intake-control/`. GET returns paginated metadata; POST performs a named, validated, CSRF-protected action. Mutations require a reason and are audited. Browser POSTs are never automatically replayed.

## Delivered controls

| Section | Available behavior | Boundary |
|---|---|---|
| Operations | Live receipt states, queue age, expired processing leases, last intake attempt heartbeat, unconfirmed delivery count, quarantine count, expiring credentials, holds, local storage capacity; filtered and paginated receipt metadata | Refreshes every 30 seconds while visible. Counts are global; client/status filters apply to the table. Last attempt heartbeat is not proof an idle worker process is alive. Disk values describe the application's intake volume, not all AWS storage. |
| Partners | Create/suspend/activate identities; approve/remove clients; exact envelope routes; workflow/action permissions; token issuance; public certificate registration, rotation, retirement and revocation; request/concurrency/storage quotas | No private keys are uploaded. A newly issued token appears once and is not persisted in browser storage. Gateway trust and certificate-chain verification remain infrastructure responsibilities. |
| Quarantine | Existing and newly rejected durable receipts, safe error codes, reviewed disposition, source/detail access through grants, eligible revalidation | Revalidation runs the normal validator. It never changes invalid content into valid content by approval. Pre-intake malformed/unauthenticated requests without a durable receipt are not turned into stored quarantine files. This is separate from orphan-storage quarantine. |
| Retention & holds | Per-client source, confirmed-output and reviewed-quarantine retention; audit/backup retention requirements; receipt/client holds and release history; bounded eligibility preview | Default is retain. Saving policy does not delete files. Physical cleanup is a separate explicit maintenance command. Audit, backups and legacy copies are not deleted by that command. |
| Access & targets | Expiring per-client staff read grants with separate download/retry flags; revocation; queue-age/RPO/RTO targets with approval references | Superadmin approval is required for grants, holds, policies and targets. No thresholds or production approvals are fabricated. |
| Recovery review | Audited delivery findings and evidence references, visible in authorized receipt detail | A finding neither resends output nor overrides completion. Uncertain side effects still need the existing reconciliation procedure. |

## Authorization and auditing

Intake receipt APIs now require an active `IntakeStaffGrant`, even for superadmins. Read access does not imply download or retry access. No existing staff accounts are automatically backfilled with PHI access. Grants expire and can be revoked. A superadmin can approve grants through the new screen; these approvals have an accountable actor, reason and expiry. There is no separate two-person approval workflow in this release.

This explicit grant boundary covers the common intake receipt/list/recovery/download/retry APIs. It is **not a complete replacement of authorization in every legacy portal module**. Existing client-user/Demo Analysis permissions and older conversion/file screens retain their existing authorization rules. Those older privileged access paths need a separate coordinated policy migration if the no-implicit-PHI rule must apply across the entire portal.

Successful receipt reads and source/output download authorizations are audited with server-generated request IDs. Authenticated legacy Django file/attachment responses are also audited without logging filenames, paths, file bytes or tokens. Download authorization is recorded before returning the response; it is not a guarantee that the browser received every byte. Downloads fail closed if the audit write fails. Files served directly by Nginx/S3 bypass application middleware and must remain private or use an independently audited authorized delivery path.

Operator-entered reasons and references must not contain patient data. The UI prompts for business reasons and ticket/evidence references. Audit data uses the existing audit protections; AWS export, retention and access controls still require operational enforcement.

## Multi-client partners and quotas

In **Partners → Manage → Add route**, choose an 835 or 837 sample to fill envelope fields automatically, then review the client, workflow and identifiers and save with a reason. A sole approved client is preselected; multiple clients require an explicit selection. Extensionless files are supported. Repeated envelope routes are collapsed; a file with several distinct routes asks which route to review. Each route is approved separately.

The preview checks complete envelopes using the existing routing parser, with a 10 MiB sample limit and at most 100 distinct routes. It detects the transaction type and requires it to be configured for the partner. Mixed 835/837 samples or malformed envelopes are rejected. Inspection does not create an intake receipt, enqueue processing, persist the source as an intake file or approve a route. Only safe preview metadata is audited. Manual entry remains available, and the normal route validation runs when Save is clicked. Preview is not full claim validation.

An identity retains its historical primary-client foreign key and can have several approved clients. The primary client can be removed from current authorization without reassigning any historical receipt. Removing a scope hides its receipts from the partner and blocks queued execution under the withdrawn scope. In-flight side effects cannot be undone.

When several active clients are approved, submissions must specify `client_id`. The selected client must match an active approved envelope route; a filename is never a routing authority. Identical active route keys cannot route ambiguously to different clients. Workflow/action permissions apply across the identity's approved client set; per-client action overrides are not part of this model.

Defaults added to existing identities are 60 requests/minute, two concurrent jobs and 10 GiB of accounted private source/output storage. Review these defaults before enabling traffic. The database serializes rate and worker-claim decisions per partner, including work for different clients. The rate limit is a fixed 60-second window, not a token bucket. All authenticated partner API requests consume it, including polling. Rate/storage rejection returns HTTP 429; workers leave jobs queued when concurrency is full.

Storage quota counts retained receipt sources and committed output checkpoints. It is not an OS filesystem quota, and temporary/unreferenced objects, legacy copies and backups require separate disk monitoring and reconciliation. Reducing quota does not prevent same-key recovery of an already accepted submission. A new oversized submission is rejected; an output quota failure is not acknowledged as successful delivery.

## Retention safety and scope

`IntakeRetentionPolicy` defaults to retaining each artifact class until explicitly approved. Cleanup only considers stopped terminal sources and confirmed completed outputs. Rejected sources must have a reviewed quarantine disposition. Client-wide or receipt-specific active holds prevent selection/deletion. A policy extension or new hold is rechecked before unlink, under the same client database lock used by hold changes.

Cleanup commits a PENDING tombstone before unlinking. Downloads of tombstoned objects are denied. After successful removal, the tombstone becomes PURGED and an audit event is recorded. A crash between unlink and DB update is recoverable by rerunning the command. Metadata, idempotency keys and audit rows remain. A pending tombstone protected by a new hold is retained, but is not automatically made downloadable again.

From the backend environment, preview a specific client before applying its approved policy:

```bash
python manage.py intake_retention --client CLIENT_UUID --limit 100
# Only after reviewing the preview and approved hold/policy scope:
python manage.py intake_retention --client CLIENT_UUID --limit 100 --apply
```

This is deletion of private intake copies only. MIR database content, normalized claims, legacy source archives, remote SFTP sources, audit exports and backups can contain additional copies. Their lifecycle/hold enforcement needs separate implementation or infrastructure configuration. Do not represent this command as complete client erasure or a complete legal-hold system across all copies.

## Deployment on AWS

1. Release the backend and frontend changes together. Take the approved database/private-storage backup and confirm restore procedures. Schedule the policy transition because existing staff receipt access will require new grants.
2. Review migrations `0067_intake_control`, `0068_quarantine_existing_rejections` and `0069_intake_control_constraints` on staging PostgreSQL. They add models, quotas, scopes and indexes; backfill rejected receipt quarantine; preserve existing source data. There is no automatic purge or grant backfill.
3. Pause intake and drain or explicitly reconcile active jobs before stopping workers for the coordinated release. Install the reviewed backend revision using the existing release procedure. With the service environment loaded:

```bash
cd /var/www/835_to_MIR_backend
source venv/bin/activate
python manage.py migrate --plan
python manage.py migrate --noinput
python manage.py check
sudo systemctl restart mir.service mir-batch-worker.service
sudo systemctl is-active mir.service mir-batch-worker.service
```

4. Deploy the frontend production build. As superadmin, open Intake Control, assign the new screen to approved administrators, create expiring client grants and verify list/detail/download/retry permissions separately. Do not use the old implicit-superadmin behavior as a success criterion.
5. Review every partner's approved clients/routes, authentication mode and quota values. The new UI manages application credential records; it does not install Nginx trust stores/CRLs, close public backend ports, rotate gateway shared secrets or provision certificates. Follow `docs/phase4-partner-security.md` and prove gateway header stripping, trusted peer enforcement, expiry/revocation and certificate rotation on AWS.
6. Enter owner-approved queue-age, RPO and RTO values with evidence references. Configure monitoring to call `python manage.py phase1_queue_status --approved-target --fail-if-unhealthy`. This is a check command, not a newly installed alarm. Connect its exit status/metrics to the approved alert system.
7. Set private-volume capacity alarms, service availability checks, credential-expiry monitoring and access-audit export. Verify the web and worker processes use the same durable database and private intake storage. No new Redis/Celery dependency is introduced.
8. Configure audit and backup retention/holds in their actual storage systems. Run restore exercises and measure achieved RPO/RTO. Entering targets in the portal does not update historical acceptance evidence or certify those targets were achieved.
9. Run the PostgreSQL concurrency suite against an **isolated staging/test database**, including the shared multi-client partner concurrency limit. Do not use production data for these tests. Use synthetic invalid files to prove quarantine, denied download behavior and revalidation.

No AWS infrastructure was changed as part of this local implementation. Do not roll back the migrations by deleting their tables after using the new controls; that would discard grants, hold/review history and quotas. Prefer a reviewed forward fix.

## Local verification

- Backend regression runner: `python scripts/run_phase1_checks.py --report PATH`. Includes real migration upgrade/reapplication coverage and synthetic security/business regression cases.
- Focused intake controls cover live counts, CSRF, screen permissions, explicit grants, expiry/revocation, cross-client denial, multi-client routing, quota/recovery behavior, quarantine review, holds, retention and safe download audits.
- Frontend: `node scripts/test-intake-control.js`, `node scripts/test-intake-history.js`, `npm run build`. Changed components pass targeted lint. Existing MappingPreview/CommonJS and bundle-size build warnings remain.
- Local checks use SQLite. PostgreSQL precision/locking tests are skipped locally and must pass in staging. AWS/load/PKI/backup acceptance is not established by these tests.
- Browser visual review was unavailable because the browser tool rejected the local `file:` preview. Automated interaction tests and the production build passed; no pixel-level visual verification is claimed.
