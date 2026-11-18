# Workspace-owned synchronization installations

This control plane records owner consent and provisions a dedicated target
after paid rights are confirmed. It issues **no worker, Resource Token or
personal API credential**. Service authentication, feed data APIs and safe
drain/manual-resume behavior are subsequent capabilities.

## Operator setup and rollout

1. Apply `f44_2802_sync_installations` before the application.
2. Configure the isolated OIDC provider and a live browser client using
   `python -m src.cli.oidc_client --help`. Its allowed scopes must include
   `openid workspaces:read integrations:manage`. Store secrets through the
   existing protected-input flow; never put secrets in argv or source.
3. Register an immutable service binding from the backend directory:

   ```sh
   python -m src.cli.sync_service register \
     --service-id catalog-sync \
     --browser-client-id catalog-console \
     --service-client-id catalog-worker
   ```

   The service ID matches the named paid entitlement. The worker client ID is
   reserved for a separate service-credential registry, not a browser OIDC
   client, secret, token or grant. Registration refuses an existing login ID.
   Identical registration is idempotent; changed client mappings are refused.
4. Enable `OIDC_ENABLED`, `MCP_PUBLIC_ENDPOINTS_ENABLED` and
   `SYNC_INTEGRATIONS_ENABLED` only after configuration/registration succeeds.
   Synchronization controls default **off**. The new controls never force
   existing memory/MCP callers to adopt OIDC.
5. Configure the trusted billing publisher with the existing private-network
   `BILLING_SERVICE_TOKEN`. It publishes finite Max/service rights using the
   [paid entitlement contract](paid-integration-entitlements.md), then invokes
   recorded-consent activation with durable retries/reconciliation.

`SYNC_AUTHORIZATION_TTL_SECONDS` is server policy: default 604800 (seven days),
minimum 60, maximum 2592000 (30 days). Browser requests cannot extend it.

## Owner control contract

All responses, including dependency/validation failures, use
`Cache-Control: no-store`. Controls accept only the isolated OIDC access-token
namespace. They reject sessions, memory API keys, Resource Tokens, worker
credentials and duplicate Authorization headers. `integrations:manage` is
required for mutations; `workspaces:read` is sufficient only for authorized
metadata inspection. Scope alone never proves ownership.

| Method and path | Meaning |
| --- | --- |
| `POST /api/v1/workspaces/{wid}/sync-authorizations` | Record finite owner consent, without creating a target |
| `DELETE /api/v1/workspaces/{wid}/sync-authorizations/{aid}` | Withdraw unused consent |
| `POST /api/v1/workspaces/{wid}/sync-installations` | Provision from exact consent and paid rights; owner-authorized alternative to internal activation |
| `GET /api/v1/workspaces/{wid}/sync-installations/{iid}` | Inspect current member/target metadata |
| `DELETE /api/v1/workspaces/{wid}/sync-installations/{iid}` | Revoke the installation and advance generation, retaining data/publication |

Approval input contains only:

```json
{
  "request_id": "2f576ccf-fde8-4d4b-8b06-74a74dfd48ba",
  "application_id": "application-123",
  "public_name": "Approved catalog",
  "public_description": "Owner-approved public description",
  "public_usage_guide": null
}
```

The authenticated browser client determines the registered service. The
request cannot choose another service/client, operation set, existing resource,
context, workspace or lifetime. The public text is explicitly approved; no
internal context summary is published as a fallback. Fixed initial operations
are `schema:read`, `records:write` and `results:read`; their data endpoints are
not implemented by this control-plane change.

Consent binds application, workspace, service/client registration generation,
operations, current approver and `ownership_epoch`. It requires both canonical
`owner_user_id` and OWNER membership. An admin, agency member, viewer or global
administrator without that exact ownership cannot approve or unlink.

The same request UUID and canonical content return the original record,
including its original expiry/revocation. Reusing the UUID with changed
authority/content returns 409. A new explicit request supersedes pending
consent for the same application/service. Withdrawal does not unlink an
already activated installation; consumed consent requires the unlink endpoint.

## Trusted billing validation and activation

These private-network commands require the billing publisher credential,
never an owner or worker token:

| Method and path | Result |
| --- | --- |
| `POST /internal/workspaces/{wid}/sync-authorizations/validate` | Validate pending consent before charging, without requiring payment or creating a target |
| `POST /internal/workspaces/{wid}/sync-installations/activate` | Recheck consent and live paid rights, then atomically provision the exact target |

Both take only the exact `authorization_id` and `application_id`:

```json
{
  "authorization_id": "4c29acd5-796d-4a30-b732-01b8239e64df",
  "application_id": "application-123"
}
```

Validation is **not a reservation**. The publisher also checks current owner,
customer and billing consent at charge time and handles authority changes
between charging and activation. Activation checks the recorded epoch,
withdrawal, expiry, current owner membership, service registration and current
browser-client registration again. It requires canonical Max (`promax`) and
both finite paid Max/service windows. Identity grant/token expiry or logout
after consent does not prevent internal activation while the consent remains
valid. A revoked browser-client registration or changed service generation
does invalidate pending consent.

The pre-charge check also refuses an already-retained installation target.
A new provision approval cannot be used to charge for an unsupported resume
or silently replace an existing target. Safe recontract/resume is a later
explicit operation, not a retry of initial provisioning.

The billing delivery/reconciler invokes activation automatically after confirmed
payment. This API is not a background scheduler and does not replace durable
publisher retries. The sync worker receives no arbitrary provisioning grant.

## Billing recovery observation

`POST /internal/workspaces/{wid}/sync-installations/observe` accepts the same exact
`authorization_id` and `application_id` references as activation. It requires the
existing billing publisher credential and the enabled sync-control surface; it
accepts no human session, browser access token or worker credential. Responses,
including failures, remain `no-store`.

The response is `sync-activation-observation-v1` and contains `workspace_id`,
`application_id`, the stored `authorization`, current `ownership_epoch`,
`binding_current`, the original `installation` or null, its `management` mode
projection or null, and `observed_at` / `valid_until`. It expires in at most
30 seconds (earlier for a pending authorization's expiry), rechecked immediately
before commit. The observation is metadata, not authority to provision or resume.

The current owner, epoch and registered service/client/generation/operation
bindings are compared under existing authority locks. A transferred owner,
transfer away and back, revoked approval, changed registration generation or
expired/consumed approval without a target returns `binding_current=false`.
A disabled/unavailable service or revoked browser registration is a conflict
hold; a caller must retain the same original references and retry after the
operator restores that registration. It must not replace the approval or target.

An unchanged consumed or expired approval can identify its existing original
target after a lost activation response. After a human unlink and reconnect the
same retained installation is bound to the newer approval: observing the original
references then returns that installation and its management projection with
`binding_current=false`, and nothing about the newer approval. This answer is
final for the original references; billing continues through the newer approval's
flow instead of retrying them. An approval only becomes an installation's original by
being consumed, which happens only in the transaction that binds the installation to
it. A never-consumed approval is therefore not credited with an installation that a
later approval of the same application created: observing it returns `installation`
and `management` null with `binding_current=false`, also while it is still unexpired,
because the retained installation means it can never activate. This answer is final
for its references too. A retained installation of another application is a
conflict and is never adopted; retrying the same references does not change that
answer. A missing physical target is reported
as `target_available=false`. Paused/revoked installation states and
`management.mode=stopping|manual` remain separate holds; an active installation
does not imply feed mode. The management projection is read under the existing
target fence using the existing default view when no mode row exists. Observation
does not enroll it or change its mode, generation, data, rights, usage or public
endpoint. Current rights are checked separately before reporting paid activation.

To activate a genuinely missing target, billing must separately confirm payment,
deliver finite rights and recheck the original review/current-owner/consent, then
use the existing paid-guarded activation POST. Reapproval/manual resume continues
through the existing human reconnect/resume flow.

## Established binding continuity

`POST /internal/workspaces/{wid}/sync-installations/observe-established` accepts
the same exact original `authorization_id` and `application_id`, with the same
billing publisher credential and enabled sync-control gate. Its separate
`sync-established-target-observation-v1` response preserves the original approval,
current ownership epoch, original installation and management projections, and
adds `continuity_current`. The existing `/observe` endpoint and its
`sync-activation-observation-v1` response remain unchanged.

Continuity ignores only the original authorizer/current-owner and original/current
ownership-epoch differences for an already consumed approval. It still checks
approval revocation, current service generation, browser and worker bindings,
operations, unchanged installation authorization, and exact available physical
resource/context/endpoint linkage. Missing or unestablished targets, a row rebound
to later consent, and changed registration cannot become continuous. A disabled
service or revoked browser registration remains a retryable conflict hold.

This is metadata, not consent to create, reconnect, bill or resume. A caller using
it for established-target confirmation must already have independently confirmed
the same original physical tuple. It must still check its own finite paid/right
evidence and explicit active installation/feed mode. Paused/revoked installations
and manual/stopping modes remain visible holds and are never changed by a read.
Both observation contracts expire within 30 seconds and are rechecked at commit.

## Pending consent for a retained reconnect

`POST /internal/workspaces/{wid}/sync-authorizations/validate-reconnect` accepts
exactly `authorization_id`, `application_id` and the retained `installation_id`.
It requires current canonical-owner pending consent, current ownership epoch,
current service/client/operations, the same application/service/worker target,
an intact physical binding, revoked installation and completed manual mode.
Initial `/validate` continues to refuse any retained installation.

The billing-authenticated read precedes funding, so it does not require the
not-yet-funded additional service right. Its `sync-reconnect-validation-v1` result
binds the pending authorization, current epoch, exact installation, manual-mode
projection and observed/valid-until timestamps. The deadline is the earlier of
30 seconds and approval expiry. A failed precondition is a fixed private
404/409; unavailable authority remains a hold. All outcomes are no-store.

Validation does not consume consent, grant rights, rebind the installation, change
generation, create a target or leave manual mode. The existing current-owner
unlink, paid reconnect and fresh exact diff confirmation remain required. A caller
must revalidate at the actual operation boundary; a saved validation is not a
durable authorization. There is no automatic replacement approval/target fallback.
Synthetic request/response fixtures for both new contracts are in
`backend/tests/fixtures/`. Deploy support before enabling a dependent caller;
a missing endpoint must fail closed, never fall back to interpreting the old
`binding_current=false` as ownership-only continuity.

## Transaction, retry and ownership boundaries

Actual provisioning enters `paid_integration_write` before every write or row
lock. Only plain reads may precede it, matching the enforced guard entry
contract. Under its Workspace lock, owner/User, membership, OIDC and service
authority are refreshed and protected with nonblocking locks. Non-paid owner
controls use the existing User fence followed by Workspace NOWAIT. A competing
erasure/authority update returns a retryable conflict instead of a lock cycle.

An outer authority context retains its synchronous `before_commit` hook until
the paid guard has flushed, rechecked paid rights and committed. Human-token
and consent expiry are checked at that final boundary after all awaited work.
Resource, public Context, search configuration, endpoint, installation, consent
consumption and audit facts commit together or roll back together. Existing
resource feature, capacity and context-quota policies still apply. Qdrant is
created lazily through the existing data path, with no remote side effect here.

The installation has one permanent workspace binding. Targets use generated
UUIDs and a unique generated slug; no existing-resource import is offered.
Concurrent activations serialize. A successful replay returns the **current**
same IDs/state, including after owner transfer, paid-window expiry or unlink;
it does not renew consent, generation or active state. Metadata is not a paid
write credential. Future token/sync consumers must enforce current rights,
service/installation state, target validity and generation themselves.

Ownership transfer invalidates pending approvals, including transfer away and
back. It preserves activated installations, which the new current owner can
inspect/unlink without the original person's login. Deleting the approver's
account clears `authorized_by` through `ON DELETE SET NULL` while preserving
the workspace-owned installation. Audit actors distinguish a human operation
from `service:billing-publisher`; owner attribution is not service impersonation.

Unlink advances generation once and preserves the resource/content and endpoint.
There is no manual-resume shortcut. Later safe resume must reuse the retained
target after its own stop/drain/diff-approval checks. A deleted, moved, private,
unpublished or otherwise mismatched target cannot be silently repaired/rebound.
Inspection reports structural `target_available`; it does not promise serving
when deployment switches, plan/capacity policy or the workspace's public usage
budget ([Public usage budget](../public-usage-budget.md), #2805) refuse it. The
installation's endpoint is an ordinary public endpoint and shares that budget;
unlink, re-activation and owner transfer leave the budget's period untouched.

**Custom domains of the installation's endpoint (#2809).** Where the operator
switched custom domains on, the canonical owner can serve the installation's
standard endpoint under their own host name, as `https://<host>/mcp`, like any
public endpoint ([Public MCP custom domains](../public-mcp-domains.md)). The
alias resolves to the same endpoint: same tools, limits, budget and usage
events. Two rights are separate and end separately:

- **Ending the sync add-on** (its license removed, the installation unlinked)
  leaves the endpoint published, so both its fixed address and its alias keep
  serving, and its data is untouched.
- **Ending the domain right** (the license `public-mcp-domains`, under
  `MCP_PUBLIC_DOMAINS_AUTHORITY=entitlement`) suspends the alias at the
  domain's next check; the fixed address, the installation and its data are
  untouched.
- **Unpublishing the endpoint** releases its custom domain in the same
  transaction; a later publication gets no alias until the owner adds and
  verifies the domain again.

The sync service id can never be `public-mcp-domains`: `sync_service register`
refuses that reserved id.

## Storage, downgrade and recovery

`sync_services`, `sync_authorizations` and `sync_installations` are operational
control records, excluded from raw-memory exports. No worker secret, token,
product/FAQ body or derived memory signal is stored in them. Historical target
and consumed-authorization UUIDs intentionally have no target-delete cascade:
a missing target/history must not free the workspace slot for automatic
replacement. Every consumer rechecks the complete UUID/workspace/slug binding.
Workspace deletion cascades consent/installations. Service records are operator
configuration and are disabled rather than deleted/rebound.

`python -m src.cli.sync_service disable --service-id catalog-sync` advances
registration generation on a real change; repeating it is a no-op. `enable`
also advances generation, so old pending consent cannot revive after re-enable.

Disable controls/publisher activity before downgrade. The migration removes
only the three control tables, leaving dedicated resources, contexts, search
configuration, public endpoints and existing audit records intact. Before
reenabling after restoration/rollback, reconcile or restore control records
with their original IDs from backup; do not auto-create replacements over
retained targets. Credential issuance, draining and production enablement
remain separate, explicit rollout steps.
