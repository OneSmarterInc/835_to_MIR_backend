# Demo accounts

Demo is a non-administrative client role (`client_role=DEMO`). Each account must
be assigned to one client when created or edited. Existing client roles retain
their permissions.

In the administrative Access screen, Admins with Access-screen permission and
Super Admins can create a user with Role **Demo**, then select its client.
The existing edit-account/profile dialog also permits changing the client.
Both user-management API families support Demo and return the Demo role label.
Existing administrator client-visibility grants still control the clients and
accounts shown in Access; creating Demo does not expand an administrator's grants.

Demo sees only Analysis and its claim detail routes. The shared Analysis API
supports uploads, validation, live upload results, comparison filters, search,
sorting, pagination, claim details, CSV exports and rejected-claim reports.
It operates on the assigned client's existing isolated Analysis tables, not a
new per-user dataset or the production conversion tables.

Backend middleware denies other business operations, including conversions,
history, dashboards, SFTP and user administration. Authentication, initial
password changes and MFA remain available. Client scoping comes from the
authenticated user's current database assignment, never a browser-selected
client. Offboarding, CSRF and MFA enforcement continue to apply.

Changing the assigned client takes effect on the Demo user's next request,
including requests in an existing session. Old-client claim and upload IDs
become inaccessible; new uploads belong to the new client. Data is not moved
or deleted. Refresh the Demo browser after reassignment to update its displayed
client name and already-rendered results.

Deployment: apply `accounts.0025_demo_role`, then deploy backend and frontend.
No Analysis data migration or backfill is required.
