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
| Active unexpired runs per installation | 4 |
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
7. Worker `POST .../runs/{run}/commit` sends the exact `diff_hash` and, when
   required, `approval_id`. Canonical approved records, all version-specific
   Resource events, the index outbox and the durable commit receipt commit
   together once. Failure rolls them all back. Changed baseline/config/schema
   or authority refuses the stale run. Another feed's records are unaffected.
8. Worker `GET .../runs/{run}` (`results:read`) recovers lost responses.
   `POST .../runs/{run}/abort` clears uncommitted payloads, retaining receipts;
   it cannot undo committed data. Repeating an identical successful commit
   returns the stored receipt; a changed commit body returns 409.

The snapshot digest is SHA-256 over UTF-8 JSON of the complete upload record
array sorted by external ID UTF-8 bytes. JSON uses sorted object keys, no ASCII
escaping, separators `,` and `:`, and rejects non-finite numbers. It hashes the
`external_id`/`fields` upload shape, not server-added fields or chunk wrappers.
The implementation helper is `models.sync_feed_schemas.snapshot_digest`.

## Canonical projection and search reflection

Human `GET /workspaces/{wid}/sync-installations/{iid}/records` returns only the
current committed approved fields, with optional `feed_id`, `limit` and opaque
`cursor`. No worker private key or permanent external body replica is needed.
The cursor binds workspace, installation, filter, schema and feed/config
revisions. A changed dataset requires restarting pagination (409). Every page
reauthorizes the current caller; it is not a public MCP call and consumes no
anonymous public MCP weekly allowance. Both human and worker surfaces use the
existing fail-closed peer OIDC burst limiter, across target/run IDs.
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
Existing indexer batch size and per-target scheduling limits still apply;
consumers must poll status rather than infer readiness from a successful commit.

The outbox is persisted in the canonical transaction. The five-minute scheduler
coalesces wakeups per target, keeps quota-held targets from starving others,
and retries an abandoned `running` state after 30 minutes. A live index batch
holds its receipt lock so recovery cannot steal it. A lost wakeup after a prior
batch commit is reasserted. The indexer refuses foreign/independently edited
Memory rows before vector changes, and skips obsolete upserts superseded by a
new canonical version/deletion. Deletions address only explicitly owned versions
which can remain visible, so accumulated retired history does not make a valid
snapshot undeletable. Existing indexer failure inspection/rewind procedures
apply to failed Resource events.

## Retention and rollback

Committed/aborted runs clear staging bodies within their transaction. Logical
expiry refuses use immediately after 24h; while enabled, periodic maintenance
physically clears expired staging in bounded batches. Stop accepting new runs
and purge expired/uncommitted staging before disabling the feature for an
extended period. Receipts retain hashes/operational IDs, not staged bodies.
Canonical records and temporary chunks are raw user data; see
[data boundary](../derived-layer-boundary.md). Workspace/resource erasure
cascades data; removing an approver clears historical user identity.

To roll back deliberately, stop new runs, drain committed indexing receipts,
disable the flag and back up feed canonical/staging tables first. Downgrading
f48 drops only the five new tables. It retains existing
Resource/Context/endpoint/credential rows and already committed Resource events
and Memories; downgrade does not reverse a published content change.
