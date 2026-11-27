# Staged product and FAQ synchronization

Issue #2807 adds full snapshots for one feed within an existing workspace-owned
installation. The server chooses the Resource, Context, endpoint, document ID,
versions and deletion namespace. These endpoints cannot issue API keys or select
another target. Multiple product/FAQ feeds share the installation's Resource.

## Rollout and authority

Apply `f48_2807_sync_feed_runs` before enabling `SYNC_RUNS_ENABLED=true` (default
false). Existing OIDC, synchronization-control and service-credential switches
must also be enabled for worker calls. Register the browser and service clients
using the existing [credential runbook](sync-service-credentials.md).

In the combined application checkout, run `alembic upgrade head`. The no-op
merge `f50_2807_merge_sync_heads` joins the feed migration with
`f49_2817_clear_allowlist_adders` (which follows the public usage-budget
migration). Either deployed parent, their common ancestor or both parent
markers can upgrade normally; do not stamp or renumber existing revisions.
The merge itself rewrites no tables, counters, period boundaries or feed data.
For a live database at `f47` or the sync-only branch, first run
`alembic upgrade f49_2817_clear_allowlist_adders` and commit, then upgrade head.
From v0.164.0 `alembic upgrade head` continues past the merge to
`f51_2809_public_mcp_domains`, `f51_2808_managed_sync` (its foreign keys take
locks on `sync_installations`, `users` and `memories`, and on the feed tables
`sync_feeds` and `sync_runs`) and the no-op merge
`f52_2808_merge_feed_domains`, the single head.
This separates the public-budget and staged-feed foreign-key locks; older
installations should follow the staged upgrade recipe in
[deployment.md](../deployment.md#how-each-account-was-admitted-issue-2803).

The browser client must explicitly register and request `sync.records:read` to
read managed bodies. Existing grants and `workspaces:read` do not gain data
access on refresh. The consent screen describes this separate permission.
Human feed configuration and destructive approval require `integrations:manage`.
Every human operation reloads the live token, grant, client, user, membership,
seat, context reach/privacy and required Context role. Configuration/approval
requires editor or owner; projection requires viewer. The browser must match
the installation service's registered browser client. Contract/payment controls
remain with the workspace owner; an invited agency editor may operate feeds.

Worker calls use only the five-minute installation-bound service token and the
existing paid transaction boundary. Current paid rights, enabled states, target
identities and service/client/installation generations are checked through actual
commit. No OIDC human token is promoted into general REST/MCP or Resource auth.

## Public fields and bounds

Products require `title` and HTTPS `url`. Optional fields are `description`,
`image_url`, `price`, `currency`, `availability`, `brand`, `sku`, `gtin`, `mpn`
and `condition`. Price is a nonnegative decimal string with up to four decimal
places and requires a three-uppercase-letter currency. Availability is
`in_stock`, `out_of_stock`, `preorder` or `backorder`; condition is `new`, `used`
or `refurbished`. FAQ requires `question` and `answer`, with optional HTTPS `url`.
Unknown fields and non-string field values are refused. The server never
retrieves a submitted URL. Search indexes approved title/description/brand/SKU
and question/answer text, without exposing other resource schema fields.

| Boundary | Initial limit |
| --- | --- |
| Feeds per installation | 20 |
| Active unexpired runs per installation | 4 (a manual run counts only while its request is current; once it is not, the run is expired and its staging cleared at the next run creation or maintenance pass, so staging stays bounded by about 4 runs x 20 MiB plus manual runs that ended since the last sweep) |
| Records per full snapshot | 10,000 |
| Upload bytes per run | 20 MiB |
| Chunks per run | 1,000 |
| Records / bytes per chunk | 100 / 512 KiB |
| Approved record payload | 32 KiB |
| Human or diff page | default 50, maximum 100 |
| Diff entries | at most 20,000 (old and new IDs may be disjoint) |
| Staging lifetime | 24 hours |
| Prepared diff / human approval | 15 minutes, within run lifetime |

External IDs are nonempty NFC text (at most 200 characters / 512 UTF-8 bytes),
without surrounding whitespace or control/format characters. IDs are exact and
case-sensitive. Server document IDs hash installation/feed/external identity;
the caller never supplies internal IDs or deletion versions.

## Protocol

All paths below start with `/api/v1`. JSON requests are streamed with a hard
byte limit before paid row locks. Duplicate keys, unexpected fields/selectors,
ambiguous credentials and invalid payloads receive fixed no-store errors.

1. A current human editor `PUT`s
   `/workspaces/{wid}/sync-installations/{iid}/feeds/{fid}` with
   `{"kind":"product","config_hash":"<sha256>"}`. The opaque hash identifies
   the source configuration, not a URL or a secret. The reply returns
   `config_version`, `schema_version` and `schema_hash`. Changed configuration
   increments a monotonic version: A → B → A cannot revive old runs/consent.
   Feed kind is immutable. The fixed schema is created only if none exists;
   an existing incompatible schema is never overwritten.
2. Worker `GET /sync/installations/{iid}/schema` uses `schema:read` to inspect
   the current approved schema and limits.
3. Worker `POST /sync/installations/{iid}/runs` uses `records:write` with
   `feed_id`, `operation_id` (UUIDs), `config_hash`, `config_version`,
   `schema_hash`, `schema_version`, `source_hash`, `snapshot_hash`, `chunks`
   and `records`. Source hash identifies the retrieved source snapshot.
   Reusing an operation ID with a different manifest returns 409. Identical
   retries return the same run, including after token/key rotation or commit.
4. `PUT .../runs/{run}/chunks/{index}` uploads
   `{"records":[{"external_id":"sku-1","fields":{"title":"Item","url":"https://brand.example/item"}}]}`.
   Chunk indices are zero-based. Identical chunk digests return their receipt;
   changed content at the same index returns 409. Cumulative bytes and record
   count are checked before storage. Neither staging nor preparation emits
   Resource events or changes canonical/published records.
5. `POST .../runs/{run}/prepare` requires every declared chunk, exact record
   count, unique external IDs and the complete snapshot digest. It binds the
   exact diff to the manifest, schema/config versions, baseline feed revision,
   generations and a fresh preparation nonce. Refresh after expiry creates a
   new diff hash and invalidates the old approval.
6. `GET .../runs/{run}/diff?offset=0&limit=50` uses `results:read`. It returns
   bounded external change IDs and before/after hashes, without bodies.
   Humans read the same preview at
   `/workspaces/{wid}/sync-installations/{iid}/runs/{run}/diff` using
   `sync.records:read`. All-delete, at least 100 deletions, or at least 20% of
   previously active feed records requires human approval. A current editor
   `POST`s `{"diff_hash":"..."}` to the human `/runs/{run}/approvals` path.
   The returned finite approval also binds the workspace ownership epoch;
   commit rechecks the approver's current membership and write permission.
   When the approver no longer qualifies (membership, Editor role, seat, plan
   or capacity), the worker's commit answers 409 `RES-002`; that is not a
   refusal of the worker, so no `sync.authority.denied` event is recorded.
   For a run declaring `manual_request_id`, approval also revalidates the
   original manual request, the requester's login (its OIDC grant) and Editor
   reach, browser client, ownership/generation and complete source version.
   The request's 15-minute deadline and the login's deadline join the approval
   transaction fence; the expiry of the access token that made the request
   does not. A different valid Editor cannot replace a lost original proof or
   extend its lifetime; approval failure rolls back identity and audit before
   success is returned.
   Automatic/unattributed approvals keep their existing behavior.
7. Worker `POST .../runs/{run}/commit` sends the exact `diff_hash` and, when
   required, `approval_id`. Canonical approved records, all version-specific
   Resource events, the index outbox and the durable commit receipt commit
   together once. Failure rolls them all back. Changed baseline/config/schema
   or authority refuses the stale run. Another feed's records are unaffected.
8. Worker `GET .../runs/{run}` (`results:read`) recovers lost responses.
   Besides `index_status` it returns `index_progress`
   (`{"indexed": n, "total": m}`, see
   [Search reflection pace](#search-reflection-pace)).
   `POST .../runs/{run}/abort` clears uncommitted payloads, retaining receipts;
   it cannot undo committed data. Repeating an identical successful commit
   returns the stored receipt; a changed commit body returns 409.

The snapshot digest is SHA-256 over UTF-8 JSON of the complete upload record
array sorted by external ID UTF-8 bytes. JSON uses sorted object keys, no ASCII
escaping, separators `,` and `:`, and rejects non-finite numbers. It hashes the
`external_id`/`fields` upload shape, not server-added fields or chunk wrappers.
The implementation helper is `models.sync_feed_schemas.snapshot_digest`.

## Current configuration and conditional updates

Normal OIDC `workspaces:read` with current Viewer reach can inspect opaque
configuration at
`GET /api/v1/workspaces/{wid}/sync-installations/{iid}/activity/feeds` and
`GET .../activity/feeds/{fid}`. These routes use the existing activity feature
gate and diagnostic authority policy, including current browser-client, member,
seat and Context reach checks. They do not request `sync.records:read`, retrieve
source URLs or expose canonical bodies/diffs/credentials. Reads neither consume
public MCP usage nor authorize a write/reconnect after revocation.

The list contains at most 20 `items`, each with `feed_id`, `kind`, `config_hash`,
`config_version` and canonical `revision`. Both responses include `workspace_id`,
`installation_id`, current `generation`, `schema_hash`, `schema_version` and
`as_of`. The individual response flattens the requested feed fields into this
snapshot. Schema hash/version are both null if absent; an incompatible existing
schema is observed, never relabeled as the approved fixed schema. There are no
query selectors. Unknown/duplicate selectors are 400, inaccessible/foreign targets
or an unknown individual feed are 404, and concurrent state locks are 409. Responses
are no-store. A 404 alone is not proof of permission to create a feed; use a
successfully authorized list snapshot to establish the current absence.

New clients must include a complete `expected` object in the human configuration
PUT, copied from the original successful snapshot:

```json
{
  "kind": "product",
  "config_hash": "<new opaque source configuration sha256>",
  "expected": {
    "config_version": 1,
    "generation": 1,
    "schema_hash": "<observed schema sha256>",
    "schema_version": 1
  }
}
```

Use `config_version: 0` only to assert that this exact feed does not yet exist.
For an absent schema, both expected schema fields must be null. Config version is
0–2,147,483,647; generation and a present schema version are 1–2,147,483,647. Values
are strict integers. Explicit null `expected`, missing bundle members, mismatched
nullability and unexpected fields are 400. All four values must match atomically
under KMC's current-authority locks before schema/configuration/success-audit
mutation. Stale create, generation/schema/config drift and ABA are 409, even when
the requested source hash currently matches. The same current hash and matching
bundle retain their version; changing a configuration at the maximum version is
409 rather than a database overflow. Current Editor and commit-time expiry checks
still apply after matching. The ordinary two-field PUT remains a legacy,
**unguarded** operation when `expected` is omitted; it is not safe to replay a
stale intent. No caller acquires a wider role by supplying a precondition.

After a lost reply or 409, read current metadata and compare it with the user's
current desired source settings and target/schema. Refreshing observations is
not consent to replay an obsolete update. A current matching configuration can
be reported as current state; there is no operation ID or receipt lookup that
proves a particular lost PUT succeeded. Do not replay an old unguarded body or
invent a new operation identity as recovery. Public's new writers always use
complete preconditions; legacy writers keep their documented unguarded behavior.

A read that still shows the snapshot a lost guarded PUT was based on does not
prove that the PUT will never apply. A delayed copy of it stays applicable for as
long as that snapshot is current and the access token that sent it has not
expired; there is no cancel operation. A PUT with the current `config_hash` does
not increment `config_version`, so sending one does not invalidate an earlier
guarded PUT with the same snapshot. Treat the outcome as unknown until the
snapshot changes or that token's expiry has passed.

## Canonical projection and search reflection

Human `GET /workspaces/{wid}/sync-installations/{iid}/records` returns only the
current committed approved fields, with optional `feed_id`, `limit` and opaque
`cursor`. No worker private key or permanent external body replica is needed.
The cursor binds workspace, installation, filter, schema and feed/config
revisions. A changed dataset requires restarting pagination (409). Every page
reauthorizes the current caller; it is not a public MCP call and consumes no
anonymous public MCP weekly allowance. Both human and worker surfaces use the
existing fail-closed peer OIDC burst limiter, across target/run IDs.
The two human reads (`/records` and `/runs/{run_id}/diff`) share the workspace
row with each other and with the activity reads, so parallel reads from one
browser or several members do not exclude each other there. An ownership
transfer, role change, ownership-epoch change, workspace deletion, a feed
configuration write or a diff approval still excludes them. The reads also hold the
installation's resource and context rows for a moment, so two of them take
turns there; a busy row is tried again for under a second before the read
answers 409 (`RES-002`).
The worker uses the fixed snapshot/chunk/event bounds and its own service
credential; it does not inherit a caller-selected Resource Token's hourly
quota. The existing Indexer workspace capacity and daily Memory-creation
quotas still apply. Proven obsolete upserts are excluded before that accounting.

Canonical commit is atomic in SQL. Search reflection is asynchronous through
the existing incremental ResourceIndexer; it is not an atomic Qdrant index
switch and can lag or apply in batches. Results distinguish `pending`, `failed`,
`complete`, `superseded`, `unchanged` and `not_committed`. An advanced event
cursor alone never proves completion: visible owned versions and deletion
postconditions must agree too. `unchanged` means the run created no index events.
While the cursor is below the run's last event, the run reads `pending` until
the indexer records one of its events as failed (in the target's
`failed_events`, see [resource indexer backfill](resource-indexer-backfill.md)),
and `failed` from then on. A full record (20 entries) whose newest entry is
older than the run's last event reads as a failure of the run too, since it no
longer keeps later ones. A run that created no index events (a re-sent
snapshot) waits on the feed's events the cursor has not passed, so it reads
`failed` while one of those is recorded as failed, and `unchanged` once they
are applied. `failed` can still change to `complete`: the
scheduler re-queues the target, and a retry that applies the event moves the
cursor past the run. An event stopped by a busy lock is not a failure; its run
stays `pending`.
Search reflection is paced by the indexer job (see
[Search reflection pace](#search-reflection-pace)); consumers must poll status
rather than infer readiness from a successful commit.

The outbox is persisted in the canonical transaction. The five-minute scheduler
coalesces wakeups per target, keeps quota-held targets from starving others,
and retries an abandoned `running` state after 30 minutes. A live index batch
holds its receipt lock so recovery cannot steal it. A lost wakeup after a prior
batch commit is reasserted. The indexer refuses foreign/independently edited
Memory rows before vector changes, and skips obsolete upserts superseded by a
new canonical version/deletion. A guard lock held by another transaction (a
status poll holds the target rows), or a held row of the version an upsert
retires, ends the batch before that event instead of failing it: the event
stays pending and the next scheduled run applies it. Deletions address only explicitly owned versions
which can remain visible, so accumulated retired history does not make a valid
snapshot undeletable. Existing indexer failure inspection/rewind procedures
apply to failed Resource events.
Within a committed replacement, owned deletion events precede new-ID upserts.
This lets a full workspace free its own capacity in one batch before a later
batch creates the replacement rows. The existing scheduler retry policy still
applies; this ordering does not promise immediate search reflection.

## Search reflection pace

The indexer job runs every five minutes on the scheduler leader and picks up to
10 queued targets, the longest-waiting first. It applies a target's events in
batches of 100 (each batch its own transaction, daily-quota charge and lock
hold) and, since v0.186.0, keeps going while events are pending: a committed
snapshot of a few thousand records is applied over a few ticks instead of one
batch every ten minutes. Each target gets up to three minutes per tick
(`DRAIN_TARGET_BUDGET_SECONDS` in `tasks/resource_indexer_job.py`) and the tick
stops starting targets after three and a half minutes
(`DRAIN_TICK_BUDGET_SECONDS`), so other queued targets keep their turn; a
target that could not start stays queued for the next tick. These are
constants, not settings. A batch starts only while one as long as the
tick's longest batch so far still ends inside both budgets, so slow batches
(slow embeddings) shorten a drain rather than run the tick into the next one.

A target whose drain used its time budget with events still pending is due at
the next tick, and that run is not refused by the per-target limits (10
minutes apart, 6 runs an hour): a drain counts as one run, and the exemption
holds only while events remain. A target with nothing pending is limited as
before. The hourly cap counts the tick that starts a drain; a continuation
moves only the last-run time, so the 10-minute interval counts from the
drain's end. The stored `applied_upserts`, `applied_deletes` and `errors` of
`indexer-status` add up the batches of that tick. During a drain each batch holds the target's locks until its commit
and releases them before the next one, with a one-second pause in between
(`DRAIN_YIELD_SECONDS`), so a status poll or feed transition waiting on them
gets its turn; a batch that meets a lock such a request holds stops there
(see below).

A process stop during a drain (a deploy, a restart) leaves the target
`running`. Its last batch commit refreshed `last_run_at`, so once that is older
than 30 minutes (`STALE_RUNNING_AFTER` in `services/sync_feed_index.py`) the
job queues the target again at its next tick and the rest of the backlog is
applied; that holds for a resource that is not a sync target too.

What ends a drain before its backlog is applied, with the same skip reason
and retry as a single batch:

- the workspace memory limit: the batch applies the prefix that fits, the
  rest waits and is retried hourly (`memory_limit_exceeded`);
- the daily Memory-creation quota: the batch is left untouched and retried
  after the next UTC midnight (`memories_per_day_exceeded`);
- a suspended plan: retried daily (`plan_suspended`);
- a busy lock: the batch stops before the event, which stays pending; the
  outbox replay queues the target at the next tick and the per-target limits
  apply to that run;
- a batch that recorded failed events (a provider outage or the embedding
  spend cap would fail the next batches too), or one that did not commit:
  the target is retried under the per-target limits.

Each drain logs one `indexer_completed` line with `batches`, `events`,
`duration_ms` and `stop` (`drained`, `held_back`, `lock_busy`,
`event_failures`, `not_committed`, `target_budget`, `tick_budget`, ...).

Progress is readable at two places. The worker's run status carries
`index_progress`: `total` is the number of index events the run committed
(its outbox receipt) and `indexed` how many of them the indexer's cursor has
passed. They are equal once the cursor passed the run, which `complete`
requires; an event the indexer recorded as failed is passed too, so
`index_status` stays the verdict. A run without index events (not committed,
or a commit that changed nothing) reads `0` of `0`, and its `index_status` can
still be `pending` while the feed's earlier events wait: `indexed == total` is
not a completion signal, `index_status` is.
The owner's `GET /api/v1/resources/{resource_id}/indexer-status` carries
`pending_events`: the resource's events after the target's cursor (all of
them before the first run), `0` once the indexer has caught up.

## Retention and rollback

Committed/aborted runs clear staging bodies within their transaction. Logical
expiry refuses use immediately after 24h; while enabled, periodic maintenance
physically clears expired staging in bounded batches. Stop accepting new runs
and purge expired/uncommitted staging before disabling the feature for an
extended period. Receipts retain hashes/operational IDs, not staged bodies.

A run that ends without committing (aborted, expired, or discarded by a
generation change) also drops its diff at once; its change count stays and is
what its status, its diff pages (now empty) and the run history report.
While `SYNC_RUNS_ENABLED` is on, the five-minute maintenance keeps finished
runs for a retention window of 30 days (`RUN_RETENTION_DAYS` in
`services/sync_feed_index.py`), in its own short transaction (a failure is
rolled back and logged as `sync_run_retention_failed`; the rest of the tick
goes on), at most 50 runs per pass:

- a run that never committed is deleted 30 days after its upload window
  closed, with its chunk receipts (and a resume candidate or manual-request
  link it still has). Its status then answers 404, it leaves the run history,
  and its `operation_id` can start a new run;
- a committed run keeps its row, its chunk receipts and its outbox receipt:
  the indexer proves every synchronized event against that receipt, also when
  an operator rewinds the cursor and events apply again, and `index_status`
  reads it. Once it was committed 30 days ago and a newer commit on the same
  feed has superseded it, only its diff is cleared; the count stays. The
  newest committed run of each feed is never touched.

Canonical records and temporary chunks are raw user data; see
[data boundary](../derived-layer-boundary.md). Workspace/resource erasure
cascades data; removing an approver clears historical user identity.

To roll back deliberately, stop new runs, drain committed indexing receipts,
disable the flag and back up feed canonical/staging tables first. Downgrading
f48 drops only the five new tables. It retains existing
Resource/Context/endpoint/credential rows and already committed Resource events
and Memories; downgrade does not reverse a published content change.

Rolling back the merge node to either explicit parent only unmerges its version
markers: both parents' schemas and data remain. Further downgrade to a shared
ancestor also affects the public-budget branch and must be planned for both
features; it is not a feed-only rollback. Prefer disabling the sync flag when
only staged-feed admission needs to stop.

From v0.164.0, `f51_2808_managed_sync` and `f51_2809_public_mcp_domains` both
follow the merge node, and the no-op merge `f52_2808_merge_feed_domains` joins
them as the single head. A plain downgrade from the combined head to the merge
node and every downgrade below it, these included, first unmerges `f52_2808`
and reverts both. It
drops the five managed-feed tables, whose foreign keys reference `sync_feeds`
and `sync_runs`: the ownership rows are lost, and the v0.164.0 application
cannot write content or list Contexts without `sync_managed_targets`, so stop
it first (see [Feed ownership and safe manual operation](feed-managed-sync.md)).
It also drops
`public_mcp_domains` with every custom domain binding: take the alias routing
out of the proxy and switch `MCP_PUBLIC_DOMAINS_ENABLED` off first (see
[Public MCP custom domains](../public-mcp-domains.md), "Stopping and rolling
back").
