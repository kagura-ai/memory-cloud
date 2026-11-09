# Workspace operation history

Operation history is a read-only page under **Workspace settings → Operation history**.
It is available to the selected workspace's owners and admins when that workspace
has the `operation_history` entitlement (Max / internal `promax`, or a custom tier
that explicitly includes it). Personal subscriptions and other workspaces do not
grant access. The API rechecks membership and entitlement on every request. The
page discards previous results when the workspace, role, or plan changes and
revalidates on focus and periodically while open.

## Recorded changes

Recording begins when this migration and the corresponding application code are
deployed. There is **no historical backfill** and no claim of a complete compliance
audit trail. Existing security logs remain independent and active on every tier.

| Area | Covered sources |
| --- | --- |
| Memories | Versioned creates, edits, soft deletes and restores through the shared revision writer; bulk forget records a count |
| Contexts | Service create, update (including privacy/sharing), soft delete and restore |
| Membership | Invitations and acceptance; workspace member additions, removals, role/access changes, and ownership transfer |
| Credentials | Member API-key issue/rotation/revocation; workspace-scoped keys revoked, deleted or regenerated from personal key settings; member OAuth-client rotation/removal; resource-token issue/revocation |
| Integrations | Connector provisioning, runtime/settings changes and deletion |
| Sleep | Completion/failure with processed-memory count; undo/rollback summaries linked to the existing report |

Sleep records a summary rather than one history row per maintenance revision.
The existing SleepAction log powers undo; the Sleep report provides the
breakdown of merges and other maintenance work.
Global credentials without a workspace, legacy unscoped data, dynamic OAuth client
registration, raw SQL/admin scripts and ordinary reads/searches are outside this
initial feed. Failure rows currently come from persisted Sleep outcomes; rejected
HTTP mutations are not synthesized into successful events. Existing security logs
continue to cover their own denied-request events.

## Attribution and privacy

The actor is the authenticated account supplied by the mutation entry point. It
is separate from the affected member and the resource owner. Agent IDs come from
verified provenance, never client-supplied display strings. Background work uses
the system surface; missing attribution is shown as unknown. API/MCP credentials
may be shared: an authenticated account does **not** prove which physical person
called it. The UI states this limitation. Surface describes the verified entry
path; browser calls without explicit session provenance may appear as API.

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
context IDs, agent IDs, operation types, and changed-field names. Combine it with
time, context, actor/agent, operation/target type and outcome filters. Pagination
uses descending UTC event time plus event UUID, so equal timestamps remain stable.
Counts use the same filters before pagination. There are no unscoped suggestions.

Successful evidence is queued and inserted in the mutation's PostgreSQL
transaction. Rollbacks discard both. Deterministic source keys use conflict-safe
inserts for retries and concurrent callbacks; mutations without an existing
idempotency contract remain distinct changes. The feed cannot edit or delete
history, and does not provide a new undo operation.

This initial version has no automatic age-based purge. Records are retained in
the deployment database; downgrading hides access without erasing records, and
upgrading restores access to retained authorized history. Account erasure replaces
actor, affected-user and context-owner identifiers with the existing salted
pseudonym. History stores no foreign key that cascades away with a deleted target.
Normal database backup/deletion policies still apply. This is not an immutable
external ledger or a certified audit retention guarantee.

## Verification

`backend/tests/integration/test_operation_history_db.py` uses a random isolated
schema under `TEST_DATABASE_URL`. Set `REQUIRE_TEST_DATABASE=1` to make unavailable
PostgreSQL fail rather than skip. It exercises privacy, canonical resource
ownership, committed/replayed/rolled-back events, filters, pagination, entitlement,
role changes, erasure and migration round trips. The history page's Vitest tests
exercise access gates, filters, pagination and stale-response handling.
