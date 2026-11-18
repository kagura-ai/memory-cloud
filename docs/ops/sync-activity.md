# Synchronization activity and authenticated manual requests

This protocol extends the existing [staged feed](staged-feed-sync.md),
[ownership/manual-mode](feed-managed-sync.md) and
[workspace public allowance](../public-usage-budget.md) contracts.
Apply `f53_2810_sync_activity` after `f52_2808_merge_feed_domains`; it adds
only `sync_manual_requests` and `sync_manual_runs`. Existing run/canonical
tables and deployed revisions are unchanged. The API is opt-in through
`SYNC_OBSERVABILITY_ENABLED` (default false), alongside the existing OIDC,
integration and run switches. Probe the authenticated API and its
`contract_version: sync-activity-v1`; a software version alone does not prove
that the operator has enabled the surface.

## Member API

Base: `/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/activity`.
All routes require explicit KMC OIDC identity access tokens from the
installation's registered browser client. Sessions, ordinary API keys and
worker tokens are not substitutes. `GET` requires `workspaces:read` plus current
workspace membership, seat and normal Viewer access to the fixed context.
The manual request route requires `integrations:manage` and current Editor
access. These metadata routes remain readable under a capacity hold or after
the paid license/installation/service is disabled; they do not grant content
access or service execution. Revoked membership, context reach, login or
client bindings still prevent inspection. Every response/error is no-store.

| Method / suffix | Input | Result |
| --- | --- | --- |
| `GET` | None | Contract version, installation state/generation, effective management mode/epoch/stop watermark, feed/record counts, authoritative allowance and notification signal |
| `GET /feeds` | None | At most 20 opaque feed observations (ID, kind, config hash/version, canonical revision) with the current generation, schema hash/version and UTC `as_of`; see [conditional updates](staged-feed-sync.md#current-configuration-and-conditional-updates) |
| `GET /feeds/{feed_id}` | None | The same snapshot for one feed of this installation |
| `GET /runs` | `limit` 1–100 (default 50), optional UUID `cursor` | Receipt metadata, next cursor and UTC `as_of`; search state is explicitly `not_evaluated` on a list |
| `GET /runs/{run_id}` | None | One run's admission/state/counts/times, received chunk/record counts and current search evidence |
| `GET /events` | `limit` 1–100 (default 50), optional positive integer `before_id` | Protocol-v1 audit references, next marker, retention window and UTC `as_of` |
| `POST /requests` | `{ "operation_id": "<UUID>" }` | HTTP 201; idempotent authenticated `request_id`, original deadline and installation generation |

Duplicate/unknown selectors and extra JSON fields are refused. A foreign run
cursor cannot select another installation. A syntactically valid missing or
foreign target is uniformly not found. Errors reuse the fixed sync
`{"error":"..."}` envelope, never body text or database exception messages.
Pagination rechecks current authority on every request and again at commit.

Reads share the workspace row: parallel `GET`s from one browser or several
members do not exclude each other, and neither do writes that only reference
the workspace. An ownership transfer, role change, ownership-epoch change or
workspace deletion still excludes them, and `POST /requests` keeps the row to
itself. When the row is busy the request tries again for under a second before
it answers 409 (`RES-002`), so a short holder such as a memory being saved in
the same workspace no longer turns a read into a conflict. The two feed
configuration reads (`/feeds`, `/feeds/{feed_id}`) also hold the
installation's resource row for a moment, so they take turns with each other
and with a feed configuration write instead of running side by side; the same
retry keeps a parallel one from answering 409.
Run lists defer the stored diff body and count its entries in SQL; cursor
lookups select only ordering metadata. Counts are null when not yet known,
rather than implying that no records or changes exist.

The mode is independent of authentication state and allowance. A null mode
means no effective ownership fence exists; an unenrolled legacy target that
the managed rollout conservatively fences is reported as `feed` with null
persisted epoch/watermark. Pausing or reaching a usage limit does not report
manual editing as unlocked.

## Admission, commit and search evidence

`state` is the source receipt state: `uploading`, `prepared`, `committed`,
`aborted` or `expired`. The response includes declared/received counts,
operation/feed/run IDs and UTC acceptance/commit/expiry times. It never
returns a manifest, external record IDs, source URLs, diff content or bodies.
`origin=manual` means a server-authenticated request is bound; `unattributed`
means no such proof was supplied, not proof that no person initiated the job.
The actual service/human executor is recorded independently in audit events.

Search observation is separate: `not_committed`, `pending`, `failed`,
`superseded`, `complete` or `unchanged`. `superseded` means a later canonical
revision owns that feed. A no-op commit is not automatically searchable:
prior events must have reached their canonical watermark. Completion also
checks actual current rows, embedding/version/source/author/content and
deletions; an advanced cursor or delivered outbox alone is insufficient.
Failure reasons are finite `index_pending` / `index_not_current` codes; raw
indexer failures can contain source text and are never projected here.
This is an observed SQL/index state, not a promise that the whole search
service changed atomically or remains available after the snapshot.

Allowance is read through the existing authoritative workspace budget reader.
Inspection never charges a call, creates a period or refills a counter.
`disabled` has null measurement fields, `idle` means no active fixed period,
and active/exhausted states carry remaining/reset/sequence data. All aliases
and endpoints continue sharing that one workspace row. Reached levels are
80/90/100 percent; the signal's dedupe key binds workspace, period sequence
and reached level. Public owns recipients, delivery, retries and deduplication;
KMC does not send notifications or introduce another counter/credit balance.

## Manual request provenance

The worker may pass the returned UUID as optional `manual_request_id` in the
existing run manifest. A UUID is an attribution reference, not bearer
authority. The worker still needs its own current paid installation-bound
credential and exact source/schema/configuration rights. No caller-provided
actor header, subject, email or user ID is accepted as human proof.

The server receipt binds its authenticated user/browser/token, installation
generation, ownership epoch, every configured source version and schema.
It is bound to the login that made it (the OIDC grant behind that access
token), not to the access token itself: it lasts 15 minutes from creation and
never beyond that login (`expires_at` is the earlier of the two). The access
token that made the request lasts at most five minutes; its expiry or rotation
by a refresh does not end the request. Anything that revokes the login ends
the request at once: the client's sign-out through `/oidc/revoke`, reuse of an
already rotated refresh token, a replayed authorization code, a password reset,
account erasure, or the operator revoking the client.
Creation is idempotent per installation/operation and never extends a retry's
deadline; at most four live requests may exist per installation. A request
can bind at most one logical run per configured feed. Separate feeds can
share it, consistent with the existing per-feed commit boundary.

Acceptance and later staging/preparation/commit recheck the original login
(the grant is neither revoked nor expired and still carries
`integrations:manage`), the requester's current Editor permissions, seat and
capacity, ownership/source/schema/generation and finite deadlines. A run whose
request has expired or whose login was revoked no longer counts toward the
four active runs per installation, although it stays `uploading` or `prepared`
until its 24-hour expiry; it can no longer be staged, prepared or committed.
Revocation, erasure or expiry never turns pending manual work into
an automatic run. A committed retry remains an immutable receipt; it does not
re-execute work. Without the optional field, manifests/digests and old retry
identities retain their old form. Keep the worker as executor and the verified
human as requester; approval of a destructive diff is a separate human act.
Pinned manual resume candidates retain the same original request checks at
preview, diff and confirmation, including the final commit deadline. Their
24-hour candidate lifetime cannot extend the original request's 15 minutes.
When that recheck finds the original requester no longer qualifies (login,
Editor role, seat, plan or capacity), every path answers the same 409
`RES-002`: the worker's run creation, staging, preparation and commit, and
another Editor's approval and resume preview, diff and confirmation. It is
not a refusal of the worker or of that Editor, so no `sync.authority.denied`
event is recorded against them.

## Audit and lifecycle

Synchronization uses the existing erasure-aware `audit_logs` storage, with a
fixed `sync-activity-v1` envelope. The canonical KMC human subject stays only
in its established actor column, alongside issuer/browser references. The
member API returns a salted pseudonym, never raw subjects or email. Service
execution names its actual client/key UUID; operator key actions are explicitly
operator-scoped. Manual source execution refers to the server request rather
than copying human identity into nested service metadata. Account erasure
pseudonymizes the human audit column and sets request user/token FKs to NULL.

Events cover consent creation/revocation/supersession, activation/unlink,
service/key registration/rotation/revocation, token issuance, authenticated
authority denials, feed configuration, admission/preparation/abort/commit,
destructive approval and managed-mode transitions. Each carries the available
fixed workspace/installation/resource/context/endpoint/run/feed/operation and
generation references. Mode events additionally carry mode/epoch/drain
watermark/generation. Pre-installation consent and global service key actions
have explicit workspace/service scope; member queries cannot expose another
workspace's activity. Legacy arbitrary audit metadata is not republished.

Success events commit in the same SQL transaction as the mutation and roll
back with it. Denials from already verified authority are recorded in a short,
isolated best-effort transaction after failure; unauthenticated inputs never
become actor claims. Audit unavailability cannot change a refusal into success.
Human denial records lock and verify the exact original account row before
insertion. An erased account or a newly registered account with the same
subject cannot restore the erased account's raw subject to audit history.
The fallback warning is constant and contains no request or exception text.
There are no private/public keys, tokens, authorization codes, payment secrets,
product/FAQ bodies, arbitrary headers or source URLs in this envelope.

Protocol-v1 audit and manual request records have a 90-day retention window.
Run maintenance prunes at most 500 of each per tick, even with run processing
disabled, without changing unrelated audit producers' retention. Expired
requests cannot authorize work while awaiting physical retention cleanup.
Deleting a request nulls its linking FK; the immutable non-sensitive UUID in
the run manifest and canonical source/run receipts remain. Existing raw
staging/candidate/preview lifetimes and general account/workspace erasure are
unchanged. Backups follow the existing restoration/erasure runbook; reconcile
current authority before executing restored work.

Rollout requires schema first and updated application/worker instances before
exposing the API. Disable new requests and quiesce workers before a downgrade;
drain/abort pending manually attributed runs before using an older binary.
Downgrading f53 removes attribution receipts/links, not source data, but the
history loss requires explicit operational approval. Feature disablement does
not erase audit or remove checks from already attributed work. Merge, release,
deployment and feature enablement remain separate operations.
