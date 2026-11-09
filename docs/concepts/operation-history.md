# Workspace operation history

Operation history is a read-only page under **Workspace settings → Operation history**.
It is available to the selected workspace's owners and admins when that workspace
has the `operation_history` entitlement (Max / internal `promax`, or a custom tier
that explicitly includes it). Personal subscriptions and other workspaces do not
grant access. The API rechecks membership and entitlement on every request. The
page discards previous results when the workspace, role, or plan changes and
revalidates on focus and periodically while visible. Refreshing the same view
keeps its table and keyboard focus; a failed authorization check clears its data.

## Recorded changes

Recording begins when this migration and the corresponding application code are
deployed. There is **no historical backfill** and no claim of a complete compliance
audit trail. Existing security logs remain independent and active on every tier.

| Area | Covered sources |
| --- | --- |
| Memories | Versioned creates, edits, soft deletes and restores through the shared revision writer; counts for bulk forget, context-merge copies, committed resource-indexer batches and capacity-purge chunks (separate counts per context) |
| Contexts | Service create, update, soft delete and restore; MCP updates and resource setup; CLI restore is system maintenance |
| Membership | Invitations and acceptance; workspace and context member additions, removals, role/access changes (including access-list extension), and ownership transfer |
| Credentials | Member API-key issue/rotation/revocation; workspace/public-context keys revoked, deleted or regenerated from personal key settings; member OAuth-client rotation/removal; resource-token issue/revocation, including automatic context-change revocation; share-key and observation-credential issue/rotation/revocation; connector KMC key rotation |
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
Global credentials without a workspace, legacy unscoped data, dynamic OAuth client
registration, direct SQL/database scripts and ordinary reads/searches are outside this
initial feed. Failure rows currently come from persisted Sleep outcomes; rejected
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

This initial version has no automatic age-based purge. Records are retained in
the deployment database; downgrading hides access without erasing records, and
upgrading restores access to retained authorized history. Account erasure and
admin/CLI user deletion replace actor, affected-user, context-owner and member
target identifiers with the existing salted pseudonym. Explicit hard deletion
of a workspace through these paths removes its history in the same transaction;
soft deletion does not. History stores no foreign key that cascades away with a
deleted memory or context.
Normal database backup/deletion policies still apply. This is not an immutable
external ledger or a certified audit retention guarantee.

## Verification

`backend/tests/integration/test_operation_history_db.py` uses a random isolated
schema under `TEST_DATABASE_URL`. Set `REQUIRE_TEST_DATABASE=1` to make unavailable
PostgreSQL fail rather than skip. It exercises privacy, canonical resource
ownership, committed/replayed/rolled-back events, filters, pagination, entitlement,
role changes, linked identities, erasure, non-UTC database timezones and migration
round trips. Its HTTP tests exercise the real FastAPI session dependency,
Bearer rejection, query validation, expired sessions, role removal and effective
workspace downgrade. The history page's Vitest tests exercise access gates,
credential/subject display, filters, pagination, hidden-tab polling, stable focus
on refresh and stale-response handling.
