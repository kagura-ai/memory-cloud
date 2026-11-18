# Feed ownership and safe manual operation

This opt-in protocol extends [staged feed synchronization](staged-feed-sync.md).
Apply `f51_2808_managed_sync` after the existing, unchanged
`f50_2807_merge_sync_heads`. The no-op `f52_2808_merge_feed_domains` joins it
with the parallel `f51_2809_public_mcp_domains` history; `alembic upgrade head`
applies both parents and preserves installations, ownership fences and domain
bindings. Unmerging f52 restores both parent markers without deleting data;
downgrading either parent itself removes that parent's tables and data.
`SYNC_MANAGED_ENABLED` defaults to false.
All persisted ownership fences remain enforced when this switch is false.
All application instances must run the new writer/indexer code before enabling
the switch; quiesce old workers and generic writers during enrollment. Never
roll an old application binary back over enrolled targets. A schema downgrade
removes ownership evidence and requires quiescing writers and explicit release
approval; disabling a flag is not permission to edit managed content.

## States and authority

`SyncInstallation.state` controls service authentication; it is separate from
`SyncManagedTarget.mode`:

| Mode | Generic REST/MCP/UI content writes | Service uploads | Source publication |
| --- | --- | --- | --- |
| `feed` | Refused with HTTP 409 `SYNC-LOCKED` | Allowed with current paid authority | Allowed at current generation |
| `stopping` | Refused | Refused | Refused; only accepted old-generation events drain |
| `manual` | Normal current KMC permissions | Bounded resume-candidate uploads only | Explicit current human confirmation only |

Pausing a client, a service, an installation, or a failed fetch never unlocks
content. Context identity/schema, ordinary memory creation/edit/deletion/moves,
manual ingestion, restore, rollback, Sleep and generic embedding rewrites share
the fence. Managed contexts use Sleep `skip`; the prior setting is restored on
manual entry. Access counters and erasure/retention remain separate lifecycle
operations. No caller-controlled bypass or owner impersonation exists.

An absent ownership row is conservatively treated as `feed` while the flag is
enabled. New installations enroll automatically; the first source/control
mutation enrolls an existing retained target. Status is read-only. Before an
ownership row exists, the indexer holds pending events without advancing its
cursor until enrollment. For a pre-barrier revoked installation, enrollment
drains the actual committed receipt generation rather than its already-advanced
revocation generation. Multiple historical generations require operator
reconciliation. Drain ordinary non-feed ingestion before rollout; enrollment
refuses that backlog so accepted ordinary writes are never silently discarded.
Persisted ownership also survives physical target loss, preventing generic
admin recovery from recreating a feed-owned target while the flag is disabled.

Before an operator disables the switch, enroll all legacy targets that must stay fenced.
Run enrollment under the Workspace lock using `services.sync_management.enroll`;
it does not create or rebind a resource/context/endpoint.

Every ordinary target writer and index batch takes a transaction-scoped shared
PostgreSQL advisory lock on `sync-management:<context UUID>`, even before an
ownership row exists. Transitions/enrollment take that lock exclusively using
`pg_try_advisory_xact_lock`. Conflicts return 409 for retry rather than waiting
with opposing locks. Multiple contexts are fenced in UUID order. Control/service
operations serialize at Workspace first, with authority/target locks NOWAIT;
generic writers take the target fence before side effects. Index batches retain
the shared fence through vectors and SQL commit. Existing orphan-safe, globally
fenced point cleanup runs after commit and cannot delete a current manual point.

## Human control contract

Base: `/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/management`.
Only the registered service's explicit KMC OIDC human token is accepted. Current
membership, seat/reach/context grants and Editor rights are checked on every
mutation. `GET` status uses `workspaces:read`; controls use `integrations:manage`.
Session/API keys and service bearer tokens do not become human control grants.
All outcomes are private/no-store. Foreign targets are uniformly not found.

| Method and suffix | Body / query | Result |
| --- | --- | --- |
| `GET` | None | Mode, epoch, installation generation, stop watermark/reason |
| `GET /access` | None | Versioned current-human lifecycle rights and retained target metadata |
| `POST /stop` | `operation_id` UUID, optional `expected` snapshot | Durable `stopping`; abort unfinished runs and advance generation once |
| `POST /finalize` | `operation_id` UUID, optional `expected` snapshot | `manual` only after writer exclusion and complete indexing |
| `POST /resume/preview` | None | Preview UUID, diff hash, retire/publish counts, finite deadline |
| `GET /resume/diff` | `preview_id`, `offset` (0–20,000), `limit` (1–100) | Exact current human's paginated diff |
| `POST /resume/confirm` | `operation_id`, `preview_id`, `diff_hash` | Atomic new generation and `feed` publication |
| `POST /resume/receipt` | `operation_id`, `preview_id`, `diff_hash`, `original_subject` | Read-only, typed original outcome under fresh current Editor authority |
| `POST /reconnect` | `operation_id`, fresh owner `authorization_id` | Same retained target, active authentication, still `manual` |

See [read-only outcome recovery](resume-receipt-recovery.md) for the versioned
receipt/absence contract and the additional conditions needed for safe abandonment.

For stop/finalize, send the four fields from the confirmed `GET` status as
`expected: {"mode":"feed","epoch":1,"generation":1,"stop_event_id":0}`.
The entire object is required when present: mode is `feed`, `stopping` or `manual`;
epoch/generation are positive integers, and stop_event_id is nonnegative. Numeric
fields are bounded to JavaScript's safe integer range; booleans, partial objects,
unknown fields and coercions are rejected. A first operation compares the snapshot
under the same exclusive target and workspace locks as the transition. A mismatch
returns the fixed private `409` without enrollment, successful transition audit,
operation receipt, generation change or pending-run/chunk changes. Current human
authorization and the finalize drain/indexing checks still apply.

A target without an ownership row yet reports the projected default `feed`, epoch 1.
A guarded finalize confirmed against that projection only enrolls the target and
returns the enrolled state; it never unlocks in the same operation. A target unlinked
before the ownership barrier enrolls as `stopping` (reason `legacy_unlinked`), so read
status again and confirm the finalize against that state with a new operation UUID.
An exact retry of the enrolling operation returns the same enrollment receipt.

The operation UUID binds its action, real principal and complete request body.
An exact retry returns its historical receipt before checking the current state;
it does not stop a subsequently resumed generation. Changing the snapshot or
switching a UUID between guarded and legacy bodies conflicts. Status must be read
again to display current mode. Only omission of `expected` keeps legacy unguarded
behavior and the same legacy operation identity; an explicit `"expected": null` is
rejected like any other malformed body. A status read that still shows the confirmed
snapshot does not prove that an earlier guarded stop/finalize can no longer apply:
it stays applicable until the snapshot changes or the request's token expires, and
there is no cancel operation. Public's exact-confirmation
controls must require a complete non-null snapshot and must never fall back to
an unguarded command, including against an older server.

### Readonly lifecycle access

`GET /access` accepts the ordinary registered browser client's human token with
`workspaces:read`. Its strict `sync-mode-access-v1` response contains exactly:

- `contract_version` and the current human `subject`.
- `target`: `workspace_id`, `installation_id`, `resource_pk`, `context_id`,
  `endpoint_id`, `generation` and `target_available`. No public URL, source URL,
  resource slug, records or credentials are included.
- `management`: `mode`, `epoch`, matching `generation`, `stop_event_id` and nullable
  bounded `reason`. All counters are integers in the JavaScript safe range.
- `permissions`: `read: true` for this metadata, plus boolean `stop` and `finalize`
  calculated from the current Context Editor/Owner role. A Viewer gets metadata
  with both mutation capabilities false. A workspace member or viewer role alone is
  insufficient; workspace owners and admins hold the Context Owner role on every
  Context they can reach.

This uses the same current identity, browser-client registration, membership,
seat/reach and target fence as the management controls. It deliberately applies
their existing capacity-safe policy: a capacity hold or Context write suspension
does not remove safe lifecycle access. Existing workspace/resource data `read`
and `sync.manage` flags remain unchanged. These booleans describe role capability;
they neither grant a new scope nor prove that draining/indexing can complete.
The Context is refreshed under a NOWAIT shared row lock before the final role
check. Private linked-owner reach also locks both ownership-witness link rows in
user order. Those locks remain through observation commit, so concurrent privacy
changes or unlink cannot return stale capabilities. Contention remains an error;
this does not require a live public endpoint or take the global link mutex.
Stop/finalize still require a separately authorized `integrations:manage` token
and revalidate current authority at the mutation boundary. Resume, source writes,
canonical content reads and paid service operations retain their normal gates.

The read never enrolls a target, changes generation/mode/Sleep, creates an operation
receipt or repairs missing publication. Unpublished endpoint metadata reports
`target_available: false` without granting any extra authority. An unenrolled
target reports the conservative default `feed` epoch 1 at its current generation.
The existing `SYNC_MANAGED_ENABLED` rollout switch also gates this route. Disabled
or foreign targets remain private/not-found; every outcome is no-store/no-cache.
Consumers must validate the explicit version, subject, full target and matching
generation; absence of this profile is not permission to infer Context rights or
fall back to ordinary data permissions for lifecycle operations.

Operation receipts bind the real principal, action and exact body. Repeating an
operation returns its original result without advancing generation again;
reusing its UUID with a different request is a conflict. Current authority is
still required on retries. Reconnect also requires current canonical owner
authority, fresh unconsumed owner consent for the same application/service,
current service registration and paid rights. Old activation replay never
reactivates a revoked installation.

## Stop, crash recovery and entitlement loss

The first stop snapshots the accepted event watermark and drain generation,
increments installation generation, aborts/purges unfinished staging and records
`stopping` in one transaction. New service credentials cannot publish while
stopping; old credentials/runs cannot commit after this transaction.

Finalization takes the exclusive target fence. It requires the index cursor to
reach the watermark, no recorded failed event through that point, and the latest
canonical feed rows' actual indexed/deletion postconditions to agree. Advancing
a cursor alone cannot unlock. The transaction then sets `manual`, advances the
mode epoch, records the manual event floor and restores the saved Sleep mode.
If a process dies, a vector operation fails, quota/capacity holds indexing, or
an event needs repair, the lock remains `stopping`. Repair/retry the existing
index runbook; never clear ownership metadata to force progress. A Free-plan
suspension can require restoring the plan/capacity before canonical drain.

Unlink and authoritative entitlement removal use this same barrier while
retaining data and all target UUIDs. Periodic run maintenance also detects
clock expiry and retries stopping targets. The paid commit guard denies writes
at the exact entitlement deadline even before the next maintenance tick.
Maintenance runs at the start of each resource-indexer tick as a step of its
own: an error there is rolled back and logged (`indexer_sync_maintenance_failed`)
and the tick still runs the queued indexers. Each installation is maintained on
its own, so one that fails (`sync_maintenance_installation_failed`, with its
installation id) does not hold back the others; targets of a soft-deleted
workspace are skipped.
Credential/temporary service disablement retains the lock. The public endpoint
is retained; stopping/status do not require a live service credential or paid
access, while resuming does. Revoking publication does not grant data editing.

## Resume snapshot and publication

With current paid service credentials, upload/prepare ordinary strict runs in
manual mode, then `POST /api/v1/sync/installations/{id}/resume-candidates` with
`{"run_id":"..."}`. Pinning closes/clears the upload, freeing its normal
four-run staging slot. One active candidate per feed is retained, with a
24-hour maximum lifetime. Replacement/consumption/expiry purges its payload.
The full candidate set is bounded to 20 configured feeds, 10,000 records and
20 MiB per installation. Every configured feed, including an empty one, must
have a fresh candidate at current service/client/installation generation,
mode epoch, source/config and approved schema version.

The Editor's preview binds the complete current manual rows, pending-event
watermark, candidate identities/manifests, canonical revisions, ownership epoch
and mode generation. Manual ingestion must be fully indexed first. There is
one replaceable preview per installation with a 15-minute deadline. Each page
and confirmation recheck normal per-row read/write permissions. The diff names
manual rows to retire (IDs, summaries, hashes) and strict public fields to
publish; arbitrary manual bodies never become public source fields. Inspect
the named memories in KMC before confirming when their full text is needed.
Any manual edit, source replacement, configuration/schema/generation/ownership
change, expiry or permission loss invalidates consent and requires a new preview.

Confirmation is one paid, real-human-authorized SQL transaction, with final
token/candidate/preview deadlines checked at commit. It retires current manual
content, preserving tombstones, revisions and the actual actor, queues durable
orphan-safe vector cleanup, stores exact tombstone proofs, and publishes all
approved feed snapshots with new monotonically increasing resource versions,
real run/outbox receipts and a new installation generation. It reuses exactly
the resource/context/endpoint. There is no half-committed resuming state: a
crash rolls back to manual. Old source events and pre-confirmation manual
events cannot overwrite the new generation. Exact approved manual tombstones
remain history until ordinary retention; later indexers cannot broadly take
ownership of unrelated/manual-edited rows.

The synchronous confirmation is deliberately bounded: 10,000 live manual rows,
20,000 history rows and 40 MiB of row data; at most 20,000 relevant canonical
identities per feed. Oversized/manual-foreign snapshots fail without mutation.
Export/archive or reduce history through normal KMC lifecycle operations before
retrying. Source acceptance and asynchronous search indexing remain distinct.
