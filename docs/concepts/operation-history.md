# Workspace operation history

Operation history is a read-only page under **Workspace settings → Operation history**.
It is recorded and viewable only for workspaces entitled to the `operation_history`
feature: the `pro` and `promax` tiers (Pro and Max; the OSS default labels are L and
XL) and any tier whose feature set includes it (`PLAN_<KEY>_FEATURES`). Free and
Starter (`free`, `basic`) workspaces record nothing. The page is available to the
selected workspace's owners and admins, with a browser session only. Personal
subscriptions and other workspaces do not grant access. The API rechecks
membership and entitlement on every request. The
page discards previous results when the workspace, role, or plan changes and
revalidates on focus and periodically while visible. Refreshing the same view
keeps its table and keyboard focus; a failed authorization check clears its data.

## Recorded changes

Recording begins when this version is deployed, or, for a workspace on another
plan, when it moves to an entitled plan. Whether an event is stored is decided in
the same INSERT, from the workspace's plan at write time. There is **no historical
backfill** and no claim of a complete compliance audit trail. Existing security
logs remain independent and active on every tier.

| Area | Covered sources |
| --- | --- |
| Memories | Versioned creates, edits, soft deletes and restores through the shared revision writer; counts for bulk forget, context-merge copies and committed resource-indexer batches (separate counts per context) |
| Contexts | Service create, update, soft delete and restore; MCP updates and resource setup; CLI restore is system maintenance |
| Membership | Invitations and acceptance; workspace and context member additions, removals, role/access changes (including access-list extension), and ownership transfer |
| Credentials | Member API-key issue/rotation/revocation; workspace/public-context keys revoked, deleted or regenerated from personal key settings; member OAuth-client rotation/removal; workspace OAuth clients from personal settings (create, edit with the changed field names, secret regeneration as revoke + issue, delete); resource management grants (issue, revocation); resource-token issue/revocation, including automatic context-change revocation; share-key and observation-credential issue/rotation/revocation; connector KMC key rotation |
| Integrations | Connector provisioning, runtime/settings changes, credential refresh and deletion; public MCP endpoint publication, edits and unpublication |
| Sleep | Completion/failure with processed-memory count; undo/rollback summaries linked to the existing report |

Sleep records a summary rather than one history row per maintenance revision.
The existing SleepAction log powers undo; the Sleep report provides the
breakdown of merges and other maintenance work.
Resource indexing records system work, not a claim about the person who supplied
an earlier ingestion event. Replayed or failed-savepoint events do not increment
its committed batch counts. Public MCP endpoints are anonymous publication
addresses, so their lifecycle is integration history rather than key authentication.
Observation-key rotation records the new key's issue; an old key still valid
during a grace period is not falsely described as revoked. Expiry alone does not
emit a revocation event. No-op member and connector edits emit no history.
Not recorded, explicitly:

- external provider (BYOK) API keys;
- agents and agent context bindings;
- the secret store (secrets, versions, grants);
- credentials removed as a cascade of a member's removal (the removal itself is
  recorded);
- global credentials without a workspace, OAuth clients without a workspace and
  dynamic OAuth client registration;
- legacy unscoped data, direct SQL/database scripts, ordinary reads and searches;
- capacity purges: they only run on Free workspaces, which record nothing.

Failure rows currently come from persisted Sleep outcomes; rejected
HTTP mutations are not synthesized into successful events. Existing security logs
continue to cover their own denied-request events.

## Attribution and privacy

Authentication adapters supply the principal kind and non-secret credential
public ID or registered OAuth client ID. Browser sessions use the Web surface;
Bearer credentials use API; the MCP transport uses MCP even for cookie sessions.
The UI displays a credential/client as the primary identity for programmatic
calls, with its account identity separately labelled. A shared credential's owner
is **not** asserted to be the physical operator. The affected member is shown
separately from both the actor and target. Missing evidence is explicitly unknown.
Agent IDs come from verified provenance, never client-supplied display strings.
System maintenance has no human actor or inherited request credential. Delegated
management uses the verified grant ID, not its caller-supplied actor header.

Only IDs, timestamps, allowlisted operation/outcome values, counts, and allowlisted
changed-field **names** are stored. Memory bodies, changed values, prompts, raw
queries, email addresses, credential names and secrets are not copied. Account
display names are resolved live; deleted names are not retained as snapshots.

Rows, counts and text search share the same workspace and privacy predicates.
Private-context history is restricted to that context's creator (including linked
identities), even for workspace owners/admins. Current privacy and the event-time
snapshot both apply. Opening a private context does not publish its old private
activity. Connector rebinding retains the more restrictive old/new boundary.
Detail links use the existing authorized memory and Sleep report pages.

Soft-deleted targets retain their IDs and recorded context boundary. Once a
context is permanently purged, only its recorded creator, still an authorized
workspace owner/admin, can read its events. Other admins lose that context's
history because its current privacy can no longer be verified. A purge cannot
reveal formerly hidden activity.

## Search and lifecycle

Search is bounded to 120 characters over authorized actor names/IDs, target and
context IDs, agent IDs, credential/client IDs, operation types, and changed-field names. Combine it with
time, context, actor/agent, operation/target type and outcome filters. Pagination
uses descending UTC event time plus event UUID, so equal timestamps remain stable.
Counts use the same filters before pagination and stop at 1,001 matches. The API
returns `total=1000, total_capped=true` above 1,000; the UI displays “1,000 or more”.
Pagination still reaches all authorized matches. This bounds count results, not
the cost of arbitrarily selective text predicates; large-scale query latency
still needs measurement. There are no unscoped suggestions. Display and time
filters use the user's configured timezone; the ending minute is inclusive.

Successful evidence is queued and inserted in the mutation's PostgreSQL
transaction. Rollbacks discard both. Deterministic source keys use conflict-safe
inserts for retries and concurrent callbacks; mutations without an existing
idempotency contract remain distinct changes. The feed cannot edit or delete
history, and does not provide a new undo operation.

History-write failures deliberately abort the source transaction. There is no
best-effort savepoint that silently commits a change without its evidence.
This consistency choice means a database/history-schema fault can block a covered
write until repaired. Run migrations before serving the new code. Tests inject
an actual history constraint failure and verify the source change rolls back.

## Retention, downgrade and export

Every entitled workspace keeps its history for **90 days**
(`config.retention.OPERATION_HISTORY_RETENTION_DAYS`). A workspace add-on extends
it: the effective retention is 90 days plus
`workspaces.addon_history_retention_days_bonus`, at most 3650 extra days. Billing
sets the bonus with the `history_retention_days` add-on dimension of the internal
plan push; admins grant it under **Admin → Plans → Workspaces** (`extra_history_retention_days`,
one day per unit). The API returns the effective window as `retention_days`, and
the page shows it.

Retention works like a TTL. Reads (rows, counts, search and every cursor page)
only return events inside the window, so an expired event disappears at once. A
daily job at 05:10 UTC (`tasks/operation_history_tasks.py`, leader-only and
single-flight) then deletes expired rows in batches of at most 5,000 until none
are left or its time budget is spent. Rows whose workspace row no longer exists
use the 90-day base. Extending the retention applies from then on; rows already
deleted do not come back. Removing the add-on shortens the window at once.

A downgrade deletes nothing: the rows stay and expire by retention like any
other row, nothing new is recorded while the workspace is not entitled, and
viewing needs an entitled plan again. Upgrading shows the retained rows that are
still inside the window. There is no download or export.

Account erasure and admin/CLI user deletion replace actor, affected-user,
context-owner and member target identifiers with the existing salted pseudonym,
and the pseudonym re-key after a salt rotation (`cli.rekey_erasure_pseudonyms`)
rewrites the same columns. Explicit hard deletion
of a workspace through these paths removes its history in the same transaction;
soft deletion does not. History stores no foreign key that cascades away with a
deleted memory or context.
Normal database backup/deletion policies still apply. This is not an immutable
external ledger or a certified audit retention guarantee.

## Verification

`backend/tests/integration/test_operation_history_db.py` uses a random isolated
schema under `TEST_DATABASE_URL`. Set `REQUIRE_TEST_DATABASE=1` to make unavailable
PostgreSQL fail rather than skip. It exercises privacy, canonical resource
ownership, committed/replayed/rolled-back events, filters, pagination, entitlement
at write time (Free/Starter record nothing, a tier override that grants the
feature records), downgrade and upgrade, the retention window and the purge,
role changes, linked identities, erasure, non-UTC database timezones and
migration round trips.
`backend/tests/integration/test_credential_history_db.py` covers the OAuth client
and management grant events. Its HTTP tests exercise the real FastAPI session dependency,
Bearer rejection, query validation, expired sessions, role removal and effective
workspace downgrade. The history page's Vitest tests exercise access gates,
credential/subject display, filters, pagination, hidden-tab polling, stable focus
on refresh and stale-response handling.
