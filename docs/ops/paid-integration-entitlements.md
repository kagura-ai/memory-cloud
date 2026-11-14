# Workspace paid integration entitlements

The billing publisher owns payment verification and subscription policy. KMC
stores resolved, finite rights and authorizes their use; it imports no payment
SDK, provider status, customer data or payment secrets. A successful redirect,
application approval or payment-method setup is not a paid-rights grant.

## Internal publisher contract

`GET /internal/workspaces/{workspace_id}/integration-entitlements` reads current
state; revision `0`, null metadata and an empty service list mean no snapshot
has been received. A missing or deleted workspace returns 404. The read holds
the same Workspace row lock as a publisher until its response snapshot is
assembled, preventing mixed revisions across its metadata and service rows.

`PUT` at the same URL accepts the **complete workspace snapshot** below. Both
routes require the existing `BILLING_SERVICE_TOKEN` Bearer credential. Missing
configuration fails closed (503, checked first); a missing or wrong credential
is 401. Two or more `Authorization` headers count as a missing credential (401),
even when one of them is valid; this holds on every billing and worker
service-token route and on the system-admin console in `service` mode. Browser
sessions, identity access tokens, API keys and integration tokens confer no
authority. Keep this secret in the billing
publisher only, never in an integration worker or an external application's
configuration. The existing edge rule blocks `/internal*`; call over the
private service network. GET/PUT responses are `Cache-Control: no-store`.

```json
{
  "revision": 42,
  "event_id": "delivery-unique-id",
  "source_ref": "resolved-workspace-snapshot",
  "max_access": {
    "valid_from": "2026-11-14T00:00:00Z",
    "valid_until": "2026-12-14T00:00:00Z",
    "evidence_ref": "verified-base-period"
  },
  "services": [{
    "service_id": "catalog-sync",
    "valid_from": "2026-11-14T00:00:00Z",
    "valid_until": "2026-12-14T00:00:00Z",
    "evidence_ref": "verified-service-period"
  }]
}
```

The response includes `workspace_id`, the canonical `plan_name`, current
`revision`, `event_id`, server-derived `source: external_billing`, `source_ref`,
`max_access`, sorted `services` and `updated_at`. PUT additionally returns
`applied` and `outcome` (`applied`, `duplicate` or `stale`). A duplicate/stale
delivery returns the **current** state, never the older submitted state.

- `revision` is a positive signed-64-bit **workspace sequence**, allocated
  durably by the publisher under its per-workspace serialization. It is not a
  provider event timestamp, subscription-specific sequence or delivery counter.
  Resolve the current aggregate state of all relevant contracts before emitting
  it. Gaps are permitted; a newer snapshot must be independently complete.
- `event_id` is an immutable delivery ID. Retry with the identical revision and
  content. Changed content with a reused ID, or a reused applied revision, is
  409. A previously unseen older revision is a harmless `stale` no-op. Applied
  receipts retain hashes, allowing old conflicting retries to be detected too.
- `source_ref` and each `evidence_ref` are opaque correlation references for
  publisher reconciliation. Do not put credentials, personal/payment details,
  provider objects, URLs or feed contents in them. References are 1–128 ASCII
  letters/digits/`_.:-`; service IDs are 1–64 lowercase letters/digits/`._-`.
- Windows are finite, timezone-qualified, nonempty **[start, end)** intervals.
  Equivalent offsets normalize to UTC. Up to 32 distinct services are accepted;
  unknown fields and duplicate service IDs are rejected. `max_access` is a paid
  Max interval or null, independently of the canonical workspace tier.
- Omitted services lose their license on a newer snapshot. To end only one
  service, include every unrelated license unchanged. `services: []` removes
  all service rights. `max_access: null` removes paid Max authority. These
  operations never delete content, resources or public endpoint publications.

## Publisher responsibilities and rollout

The existing plan/addon PUT remains unchanged. Continue publishing the canonical
workspace tier through that API. This API does not rewrite its provenance,
quota bonuses or tier. For a participating workspace, publish complete rights
on **every** relevant Max or service payment, renewal, cancellation, correction
and reconciliation, including when only one contract changes. Use the same
per-workspace publisher lock across both delivery paths. The current workspace
tier is an additional restriction; a legacy tier upgrade alone cannot create
rights or revive a revoked service snapshot.

Before the first verified payment, publish no active license. Set the effective
start only after verified payment success, not on browser completion. A
scheduled cancellation retains its paid window through the paid end; do not
remove it immediately. Paid renewal extends the interval in a newer snapshot
(coalesce contiguous paid periods rather than replacing a still-live start
with a future one). The billing publisher determines any explicit grace policy;
KMC never infers grace from a provider's free-form status or auto-extends time.

Read current state to diagnose missing delivery or drift. After repair or a
backup restore, emit an authoritative complete snapshot with a revision higher
than both the publisher's previous revisions and KMC's latest one. Do not reset
the sequence or replay historical events as a reconstruction strategy. Delivery
failure may delay activation; finite expiry still closes access without a
successful sweep or notification. Publisher implementation is a separate task.

## Mandatory consumer boundary

`inspect_paid_integration(db, workspace_id, service_id)` is for point-in-time
display only. It requires a live workspace, canonical tier `promax`, a current
paid Max window and the named service's current window. The earlier end wins.
Missing/unconfirmed, not-yet-started and expired rights deny, including exactly
at either end. Reads refresh ORM state and do not cache authorization results.

Installation creation, token issuance and synchronization commit handlers must
use `paid_integration_write(db, workspace_id, service_id)` after authenticating
the caller, as the transaction's **first write or row lock**. Inside the
guarded block, verify the current role, installation/generation and fixed
target; hold any additional authority-row locks through the helper's commit so
they cannot change after verification. A cached inspection outside the block
is insufficient. The helper itself grants none of those authorities. It takes
the Workspace row lock, admits work, flushes, and checks current rights and
current time again immediately before owning the final commit. It rolls back
on expiry, failure or cancellation; commits inside its body are refused. Do not
call commit/rollback, nest transactions or defer writes beyond the guarded
block. `require_paid_integration` provides the same locked initial check for
read-only admissions; keep its transaction until the action ends.

Commit the authentication's own writes before entering either one. The
standard API-key authentication writes into the request session and does not
commit: `APIKeyManager.verify_key` flushes `api_keys.last_used_at`, and for an
agent-bound key `AgentRegistryService.touch_last_seen` updates
`agents.last_seen_at`, each once per throttle window (always for a key that was
never used). Those writes give the transaction an id, so without an
`await db.commit()` between authentication and the entry, the entry refuses
exactly the API-key callers whose bookkeeping was due; cookie sessions and
requests inside the throttle window pass, which hides the bug in tests. The
order is: authenticate, `await db.commit()`, then `paid_integration_write` or
`require_paid_integration`.

The entry order is enforced. Both entries refuse, with `RuntimeError` and
before they lock, register a commit hook or roll anything back (the caller's
transaction is left to the caller):

- a session with pending ORM changes;
- a transaction that already has a PostgreSQL transaction id
  (`pg_current_xact_id_if_assigned()` is not `NULL`): any earlier write or row
  lock (`FOR UPDATE` / `FOR NO KEY UPDATE` / `FOR SHARE` / `FOR KEY SHARE`),
  including the authentication bookkeeping above, and also a temp table or a
  WAL-logged `nextval`. These last ones fail closed.
- an `ACCESS EXCLUSIVE` table lock, but only when `wal_level` is `replica` or
  `logical` (the default): PostgreSQL then assigns a transaction id to log the
  lock for standbys. Under `wal_level=minimal` it assigns none and the lock is
  not detected.

Earlier plain reads are fine, including `inspect_paid_integration`. Advisory
locks, table locks below `ACCESS EXCLUSIVE` and, under `wal_level=minimal`,
`ACCESS EXCLUSIVE` itself assign no transaction id and are **not** detected; a
caller that takes one first follows the global lock order in
`backend/tests/integration/test_erasure_lock_order_db.py`. It is one
admission per transaction: a second `require_paid_integration`, or a call
inside `paid_integration_write`, is refused, because the first one already
holds the Workspace row lock.

The common lock order is **Workspace → entitlement/installation/resource rows**,
and a transaction holding a `users` row never waits on a workspace row (the
rule of `test_erasure_lock_order_db.py`). The deadlock partner of a caller that
locked its `users` row first would be a workspace → users path such as
`WorkspaceService.delete_workspace`; the entry check refuses such a caller.
Publisher updates take the same Workspace lock. An in-flight writer that owns
it either completes its final valid gate and commits before revocation, or a
revocation that owns it first makes the writer fail. Previously issued tokens
and earlier inspections cannot bypass the new gate. Expiry uses current wall
time after all awaits, not PostgreSQL transaction-start `now()`; its security
boundary is the final authorization immediately before commit, not the later
network acknowledgement. No background job or cached token claim grants time.

Concrete installation, token and sync HTTP consumers are added by their later
implementation tasks; this change adds no placeholder privileged endpoints.

`backend/tests/api/test_paid_integration_route_table.py` enforces the boundary:

- **Router table** (`PAID_INTEGRATION_ROUTERS`): every router module that serves
  a REST route in the app carries a decision, `NONE` (grants no paid-integration
  authority) or `GUARDED` (names the service module whose code calls
  `paid_integration_write` or `require_paid_integration`; the router imports it).
  A new router module without a row, or a row whose module no longer serves a
  route, fails. The table works per module: a paid route added to a `NONE`
  router is caught only through the symbol scan below.
- **MCP tool set** (`PAID_INTEGRATION_MCP_TOOLS`): every MCP tool carries a
  decision too; all are `NONE` today.
- **Symbol table** (`PAID_INTEGRATION_SYMBOLS`): an AST scan of every module
  under `backend/src` lists each one that imports
  `services.integration_entitlements` or `models.integration_entitlement`
  (relative imports resolved), uses a name of the service through a module
  object, names one of the three entry functions, references or defines a
  mapped paid-integration class (a `WorkspaceIntegration*` class or a
  `workspace_integration_*` table), or names those modules or a mapped
  `workspace_integration_*` table in a string (docstrings and other text such
  as log event names do not count). Each such module has a role, and a role
  allows only its kinds of use. Outside the service no role allows a module
  import of the service: a consumer imports the names it uses, so a private
  helper (one that skips the entry-order check, say) can never be reached
  unnoticed. A `guarded` module imports `paid_integration_write` /
  `require_paid_integration` by name; a module outside the service that names
  `inspect_paid_integration` must be `display_only`, so a cached inspection
  used to admit a write needs a visible table change. Consumer tables
  therefore live in `models/integration_entitlement.py`. Background tasks and
  other non-route code are covered by this scan only.

## Storage and migration

`f43_2798_paid_integrations` adds current workspace state, named service licenses
and immutable applied-event receipts. All are operational control-plane data,
excluded from raw-memory export. They contain no owner identity; ownership
transfer preserves the workspace's rights. Workspace deletion cascades them.
The migration's downgrade removes these tables only, preserving workspace and
content data. Deploy the migration before the new application; activating the
publisher and subsequent consumers is a separate rollout.
