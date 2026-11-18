# Ordered workspace billing snapshots

Payment owns provider verification and policy. KMC persists resolved finite
Max/service rights and optional plan/quota changes under one workspace sequence.
There is no payment SDK, provider object, browser authority or setup grant here.

## Contract

`GET` and `PUT /internal/workspaces/{workspace_id}/billing-snapshot` require the
existing internal billing publisher credential. They are private-network APIs,
blocked at the edge. Missing configuration is `503`; absent, wrong or ambiguous
credentials are `401`. Responses use `Cache-Control: no-store` and
`Pragma: no-cache`.

The shared wire is frozen in
[workspace-billing-snapshot-v1.json](../../backend/tests/fixtures/workspace-billing-snapshot-v1.json).
`PUT` requires `contract_version: workspace-billing-snapshot-v1`, `revision`,
`event_id`, `source_ref`, `max_access`, the complete `services` list and `plan`.
The `plan` key is required, and its value has two meanings:

- `plan: null`: replace the finite rights under this revision while preserving
  `plan_name`, every addon column and `entitlement_source` exactly as stored.
  Payment must use this form when the canonical `entitlement_source` is
  `admin_grant`, so a rights update does not end an administrator's grant.
- A non-null `plan`: set `plan.plan_name` to `free`, `basic`, `pro` or `promax`,
  replace the addon columns and set `entitlement_source` to `external_billing`.
  This is an explicit billing takeover, as with the legacy plan route.

A non-null `plan.addons` must contain exactly the twelve frozen v1 dimensions:
`memory`, `mcp_quota`, `rest_quota`, `public_quota`, `member`, `context`,
`analysis`, `storage_mb`, `sleep_contexts`, `connector`, `memories_per_day` and
`history_retention_days`. These keys are independent of the live storage map.
A non-null v1 plan sets any later storage dimensions outside that set to `0`;
`plan: null` preserves those dimensions too. Adding a storage dimension does not
change which requests v1 accepts. Partial/extra maps and bool/float quantities
are refused. Bonuses are `0..2,000,000,000`; `history_retention_days` is at most
`3650`. Existing finite-window, unique-service, reference and signed-64-bit
revision bounds still apply. No provider identifiers, URLs or secrets belong
in this request.

The response always reports the current, non-null `plan`, `ordered_billing` and
canonical workspace `entitlement_source`, distinct from the integration state's
`source`. Read models report stored tier/provenance strings and the live addon
map without imposing v1's write vocabulary or bounds. This allows future or
historical stored values to be inspected without a `500` or rolling back a valid
rights-only update. Reporting a value does not authorize paid use of that value.
`PUT` also returns `applied` and `outcome`. A duplicate/stale delivery reports the
current whole state, which can be newer than the submitted delivery.

## Ordering and opt-in

Both forms share the existing workspace row lock and transaction. The digest
covers the actual validated complete body, including `contract_version` and
`plan: null` when present. Retry retains the original event, revision and body.
Reusing an event/revision across null and non-null forms is `409`; an unseen
older revision is a stale no-op. Increasing revisions can switch forms.

The first successfully applied complete delivery, including `plan: null`, sets
`ordered_billing=true`. Reading, deployment, integration-only pushes and stale
complete requests do not opt a workspace in. Migration `f54` defaults existing
rows to `false`; its UPDATE trigger rejects clearing the flag or moving the row.

An opted-in workspace rejects legacy `PUT /plan` and integration-only
`PUT /integration-entitlements` before mutation, including requests queued behind
opt-in's row lock. The stable response is `409`, error `RES-002`, with
`details.reason: ordered_billing_required`. It is distinct from event/revision
content conflicts. A legacy publisher must switch to the ordered protocol,
without retrying a legacy write indefinitely. Each refusal logs one INFO event
`ordered_billing_refused` with the workspace ID and route. A non-null applied
plan logs `workspace_billing_plan_applied` with old/new tier, addon map and
canonical provenance. Rights-only updates do not log a plan change.

Existing reads and administrator operations remain available. Unmanaged
workspaces keep their previous behavior. No content, resources, installations,
endpoint identities or usage counters are recreated. Quota/rights changes do not
start a new public usage period. Normal admission and stop-ineligible behavior
still apply.

A delivery that ends a paid integration right also stops that workspace's
synchronization in the same transaction. When the stop has to wait for another
transaction, a workspace whose installation has a managed-target row still gets
its normal `200`: the stop is finished by the next maintenance tick. An
installation from before feed management (no managed-target row, with
`SYNC_MANAGED_ENABLED` on) cannot be left for maintenance, so the delivery is
refused before anything is kept: `409`, error `RES-002`, with
`details.reason: sync_transition_busy`, logged as `sync_stop_refused`. It is
retryable, unlike the event/revision content conflicts and
`ordered_billing_required`: keep the saved body and retry it exactly, with
backoff. It is never a `404` for an existing workspace. A retry that keeps
answering it (for example, the installation's Context was deleted while
management was off) needs the sync operator, not a different body.

A missing or soft-deleted workspace returns `404` / `RES-001` from the snapshot
read/write. This is final for a known deleted workspace: the publisher records
its cancellation and stops applicable billing, as for the existing entitlement
read; it must not keep retrying to force a `free` snapshot. An ordered deleted
workspace's legacy `free` push remains `409` with the ordered-protocol reason.
Only a verified application-level workspace-not-found response after endpoint
readiness is established has this meaning. A generic proxy/old-server `404`
does not prove deletion or authorize a financial cancellation.

## Coordinated rollout and recovery

This protocol does not enable itself. Payment #201 must deliver its complete
durable publisher before a live workspace opts in.

1. Apply `f54` and deploy #2882 to every serving backend, including both colors.
   Drain older instances and in-flight old-version requests before opt-in. API
   fences require this release; SQL's flag guard cannot fence an old binary.
   A successful response from one color does not prove whole-fleet readiness.
2. Keep Payment feature flags off while testing isolated credentials, the exact
   shared wire, rollback and honest signed/provider-reconciliation evidence.
3. Route every relevant Max/service change through one durable publisher.
   Allocate revisions above local and KMC watermarks, save the whole immutable
   body before delivery and retry it exactly. Hold no SQL lock across a network
   call. Use `plan: null` for `admin_grant` rather than copying the admin plan
   into a non-null request, which would transfer provenance to billing.
4. Use verified finite coverage; preserve paid gaps, unrelated services and
   scheduled paid ends. Active flags, setup returns and browser success pages
   are not payment evidence. Read responses are not activation authority.
5. After opt-in, do not roll KMC back to a pre-fence binary or remove `f54` while
   publishers can run. Never clear delivery state or reset revisions. The trigger
   guards UPDATE only: deleting `workspace_integration_states` clears the mode
   and cascades to its licenses and event receipts, erasing ordering protection.
   It is not a recovery procedure.
6. Keep the saved body on failure; inspect the current whole state. After restore,
   re-resolve current authoritative state before allocating a higher revision;
   do not retime old paid periods or blindly rebase an old desired state.

There is no supported API or operator SQL procedure to reverse an accidental
opt-in. Do not disable its trigger or delete/rewrite state. Stop and drain every
publisher, involve the KMC and Payment operators, preserve flags/receipts and
inspect original saved deliveries. Recover forward using a corrected complete
state and a higher durable revision after rechecking authority. Keep the
workspace opted in. Any exceptional database repair requires a separately
reviewed coordinated migration and consistent backup/restore plan, followed by
verification of plan/provenance, all quotas, rights, receipts and both watermarks
before resuming publishers; it is not an automatic rollback path.

These steps require operational acceptance. This PR performs no deployment,
secret distribution, Stripe writes, production opt-in or billing cancellation.
