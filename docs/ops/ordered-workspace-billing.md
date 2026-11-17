# Ordered workspace billing snapshots

Payment owns provider verification and policy. KMC persists a resolved complete
plan, addon quotas and finite Max/service rights, with one workspace sequence.
There is no payment SDK, provider object, browser authority or setup grant here.

## Contract

GET and PUT /internal/workspaces/{workspace_id}/billing-snapshot require the same
internal billing publisher credential as the existing integration entitlement
API. They are private-network endpoints, blocked at the edge. Missing
configuration is 503; absent/wrong/ambiguous credentials are 401. Responses use
Cache-Control: no-store and Pragma: no-cache.

The strict wire is frozen in
[workspace-billing-snapshot-v1.json](../../backend/tests/fixtures/workspace-billing-snapshot-v1.json).
PUT contains the existing revision, event_id, source_ref, max_access and complete
services list, plus:

- contract_version: workspace-billing-snapshot-v1
- plan.plan_name: free, basic, pro or promax
- plan.addons: all twelve canonical dimensions, including explicit zeroes:
  memory, mcp_quota, rest_quota, public_quota, member, context, analysis,
  storage_mb, sleep_contexts, connector, memories_per_day, history_retention_days.

Unknown fields/dimensions, partial addon maps and booleans/floats as quantities
are refused. Bonuses are 0..2,000,000,000; history_retention_days is at most 3650.
Existing finite-window, unique-service, reference and signed-64-bit revision
bounds still apply. No Stripe identifiers, URLs, statuses or secrets belong in
the request.

GET returns the current integration view together with plan, ordered_billing
and the canonical workspace entitlement_source. The latter distinguishes an
explicit administrator grant from billing ownership; it is distinct from the
integration snapshot's source. PUT additionally returns applied and outcome.
A duplicate/stale delivery returns the current whole state, including plan and
quotas, rather than echoing its submitted state.

## Ordering and opt-in

The complete plan and service state share the existing workspace row lock and
one transaction. The immutable delivery digest includes the plan, all quotas and
all rights. Exact retry retains its event ID, revision and full content. Changed
event/revision reuse is 409; an unseen older revision is a stale no-op.

The first successfully applied complete snapshot sets ordered_billing=true.
Reading, deployment and old integration-only pushes do not opt a workspace in.
A stale complete request also does not opt it in. Migration f54 defaults all
existing rows to false; a database fence rejects clearing an opted-in flag or
moving it to another workspace.

For an opted-in workspace, legacy billing PUT /plan and integration-only PUT
/integration-entitlements return 409 before mutation, even if their request
waited behind the opting-in transaction's row lock. Existing reads remain
available. Unmanaged workspaces keep their previous write behavior. Explicit
administrator operations remain available; they do not clear ordered mode.

No content, resources, installations, endpoint identities or usage counters are
recreated. A quota/rights update never starts a new public usage period. Normal
paid-integration admission and stop-ineligible handling still apply.

## Coordinated rollout and recovery

This is an opt-in protocol, not automatic enablement. Payment #201 must deliver
the durable complete publisher before any live workspace opts in.

1. Apply f54 and deploy the KMC release containing #2882 to every backend
   instance that can receive billing traffic, including both deployment colors.
   Drain older instances and in-flight old-version requests before opt-in.
   The API-level legacy fences require that release; an older KMC binary does
   not know them. A new-route response from only one color is insufficient
   evidence that the whole fleet is ready.
2. Keep the Payment feature flags off while configuring and testing isolated
   credentials, exact wire compatibility, rollback and signed invoice evidence.
3. Route every relevant Max/service change through one durable workspace
   publisher. Allocate revisions durably above both local and KMC watermarks,
   persist the complete immutable body before delivery, and retry that exact
   body. Do not hold SQL locks across network calls.
4. Use verified finite receipt coverage, retain gaps and unrelated services, and
   preserve paid windows at scheduled cancellation. A subscription active flag,
   setup return or browser success page is not payment evidence.
5. After first opt-in, do not roll KMC back to a pre-fence binary or remove f54
   while billing requests can run. Stop/drain every publisher first and plan
   operator-controlled migration; clearing local delivery state or resetting
   revisions is not a recovery procedure.
6. On delivery failure keep the saved body. Diagnose KMC's current whole state.
   A repair after restore must re-resolve current authoritative complete state
   before allocating a higher revision, rather than retiming old paid periods.
   Respect canonical administrator provenance in automated reconciliation.

These steps require operational acceptance. The implementation and tests do not
perform deployment, secret distribution, Stripe writes or production opt-in.
