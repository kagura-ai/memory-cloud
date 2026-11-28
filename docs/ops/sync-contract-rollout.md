# Synchronization contract and staged rollout

Issue #2811 freezes the KMC-side contract used by Public. It adds discovery and
acceptance fixtures, not a deployment or feature enablement. Existing memory,
session, Resource Token and MCP OAuth users do not need the new OIDC login.

## Negotiate before starting work

Fetch `GET /.well-known/kmc-sync-configuration` from the explicitly configured,
trusted HTTPS KMC origin. The public response is `Cache-Control: no-store` and
`Pragma: no-cache`; it contains no tenant identity, entitlement, key, host-derived
origin, target URL, database state or grant. Do not derive its origin from an
untrusted feed, Host header, redirect or metadata document.

`protocol=kmc.sync.v1`, the advertised record schema pair (`schema_version`,
`schema_hash`), `human_scopes`, `service_scope=resource.sync` and
`assertion_audience=issuer` identify the supported wire profile. The advertised
pair is one of the published pairs: version 1
(`bdb55d3bdbbbb72a92a33fb96e6a2dc60ccbaf5b09638dcc1f1619d016dfec0d`) or, while
`sync.schema.v2` is advertised, version 2
(`8ddfbbbde388dd0ac44899e10be2a6c26d0a4521098a9bd59f74784a59bb3834`). It is the
version a **new** installation gets; a consumer names the pairs it accepts and
compares `capabilities` as a set. What one installation holds is read from its
own `/schema` and `/activity/feeds`. `capabilities` lists dependency-aware
enabled profiles. It is not a health/readiness certificate: migrations, signing
configuration, browser/service registration and current authorization are
separate checks. Disabled deployments still describe the compiled profile but
advertise no enabled capabilities. A release version is never evidence of support.

| Capability | Required deployment switches |
| --- | --- |
| `identity.oidc.v1`, `workspace.access.v1` | `OIDC_ENABLED` |
| `public.mcp.v1` | `MCP_PUBLIC_ENDPOINTS_ENABLED` |
| `sync.installations.v1` | Identity, public MCP, `SYNC_INTEGRATIONS_ENABLED` |
| `sync.credentials.v1` | Installations, `SYNC_CREDENTIALS_ENABLED` |
| `sync.feeds.v1` | Credentials, `SYNC_RUNS_ENABLED` |
| `sync.managed.v1` | Feeds, `SYNC_MANAGED_ENABLED` |
| `sync.activity.v1` | Installations, `SYNC_RUNS_ENABLED`, `SYNC_OBSERVABILITY_ENABLED` |
| `public.budget.v1` | Public MCP, `MCP_PUBLIC_USAGE_BUDGET_ENABLED` |
| `public.domains.v1` | Public budget, `MCP_PUBLIC_DOMAINS_ENABLED` |
| `public.domains.observation.v1` | Installations, public budget, `MCP_PUBLIC_DOMAINS_ENABLED` |
| `public.domains.operations.v1` (experimental) | Domain observation, `SYNC_PUBLICATION_DOMAIN_OPERATIONS_ENABLED` |
| `public.text.operations.v1` (experimental) | Installations, `SYNC_PUBLICATION_TEXT_OPERATIONS_ENABLED` |
| `public.usage.stats.v1` (experimental) | Installations, `SYNC_PUBLICATION_USAGE_STATS_ENABLED` |
| `sync.schema.v2` | Feeds, `SYNC_SCHEMA_V2_ENABLED` |

These profiles conservatively describe support for complete consumer workflows;
they do not remove diagnostic reads which intentionally survive worker shutdown.
Public's standard-URL profile requires every listed capability except the
three `public.domains.*` ones. Starting without a custom domain must not wait for optional DNS/TLS
setup. A domain operation additionally requires `public.domains.v1` and its
current independent entitlement/proof/TLS checks. Reading an installation's
domain through scoped OIDC needs `public.domains.observation.v1`; claiming,
verifying and removing it through the installation's browser client needs
`public.domains.operations.v1` (experimental, optional for a consumer).
Setting the public description and usage guide of the installation's endpoint
through the installation's browser client needs `public.text.operations.v1`
(experimental, optional for a consumer, #2995). Reading the usage statistics
of the installation's endpoint through the installation's browser client needs
`public.usage.stats.v1` (experimental, optional for a consumer, #3035). A
client that does not see a capability keeps to the paths it already supports.
`sync.schema.v2` (#3055) means this deployment stores record schema version 2,
gives it to new installations and serves the schema upgrade; it is always the
last element of `capabilities`, and the advertised pair is version 2's exactly
while it is present.

The producer model is `models.sync_contract.SyncConfigurationV1`. The pure
`services.sync_contract.require_sync_contract` helper is an executable consumer
reference; it performs no network I/O and grants no authority. It rejects a
missing/malformed document, a changed protocol or auth profile, a schema pair
the consumer did not name (`accepted`, version 1 only by default) or one that
disagrees with `sync.schema.v2`, missing human scopes or required capabilities
with fixed `unsupported_sync_contract`.
Unknown capability names do not satisfy a required name. Version1 document
fields are fixed; new fields or incompatible semantics require an explicitly
negotiated contract version. No validation error should be shown with the raw
untrusted document attached.

Consumers must use bounded time/size, require HTTP200 JSON, reject redirects,
and stop on timeout, TLS/HTTP/JSON failure or unsupported configuration. Never
fall back to a version threshold or stale metadata. Re-fetch before admitting
new work and after reconnection/reconfiguration. Verify the configured issuer's
OIDC discovery and complete current membership/installation/service admission
before queuing a run. The live request and final-commit fences remain authoritative
if configuration or authority changes after negotiation. Integration in the
Public application and its live end-to-end acceptance remain Public-side work.

## Frozen API, scope, schema and error boundaries

All paths below are prefixed `/api/v1` except the OIDC endpoints. Human scopes
must be explicitly registered and granted; scope never substitutes for current
membership, seat, Context rights or canonical ownership. An agency Editor can
operate feeds; only the current brand owner controls consent/contracts.

| Surface | Authority | Authoritative contract |
| --- | --- | --- |
| `/oidc/*` | Identity-only authorization code/PKCE; isolated from MCP OAuth | [OIDC provider](oidc-provider.md) |
| `/me/workspaces`, `/me/workspaces/{wid}/access` | `workspaces:read`, current membership | [Workspace access](oidc-workspace-access.md) |
| `/workspaces/{wid}/sync-authorizations`, `/sync-installations` | `integrations:manage`, current owner and recorded paid consent | [Installations](workspace-sync-installations.md) |
| `GET /workspaces/{wid}/sync-installations/{iid}/publication-domains` | `workspaces:read`, current canonical owner | [Domains](../public-mcp-domains.md) |
| `POST /workspaces/{wid}/sync-installations/{iid}/publication-domains/claim`, `/verify`, `/remove` (experimental) | `integrations:manage`, current canonical owner, the confirmed binding | [Domains](../public-mcp-domains.md) |
| `POST /workspaces/{wid}/sync-installations/{iid}/publication-text` (experimental) | `integrations:manage`, current canonical owner, the confirmed binding, installation not unlinked | [Installations](workspace-sync-installations.md) |
| `/sync/installations/{iid}`, `/schema`, `/runs/*` | Installation-bound worker token; `schema:read`, `records:write`, `results:read` | [Credentials](sync-service-credentials.md), [feed protocol](staged-feed-sync.md) |
| `POST /workspaces/{wid}/sync-installations/{iid}/schema/upgrade` | `integrations:manage`, current Context Editor; served while `sync.schema.v2` is advertised | [Feed protocol](staged-feed-sync.md#record-schema-versions) |
| Human `/workspaces/{wid}/sync-installations/{iid}/records` and diff | `sync.records:read`, current Context read rights | [Feed protocol](staged-feed-sync.md) |
| Human feed config/approval and `/management/*` | `integrations:manage`, current Editor; ownership where required | [Managed data](feed-managed-sync.md) |
| Human `/activity`, `/activity/feeds`, `/activity/runs`, `/activity/events` | `workspaces:read`; manual requests additionally `integrations:manage` and current Editor | [Activity and audit](sync-activity.md) |
| Anonymous public MCP and verified aliases | Same workspace counter, per-minute protection, approved public target | [Budget](../public-usage-budget.md), [domains](../public-mcp-domains.md) |

The service grant uses `resource.sync`, RS256 client assertions with exactly the
configured **issuer** as `aud`, and five-minute installation-bound tokens. It
does not turn OIDC human credentials into memory REST/MCP credentials.

`backend/tests/fixtures/sync-contract-v1.json` is synthetic consumer material:
the full enabled document plus strict product and FAQ uploads.
`sync-contract-v2.json` is the same with `SYNC_SCHEMA_V2_ENABLED` on, plus
version-2 product, page and review records. `sync-schema-v1.json` and
`sync-schema-v2.json` hold the exact bytes of the worker `/schema` answer of an
installation on each version; tests compare the answers byte for byte. Each
schema hash is frozen in `models.sync_feed_schemas` (the registry); changes to
the public field contract require an intentional consumer compatibility update. This
document's schema version identifies the wire profile; each run still uses
the current installation schema/configuration revisions returned by its own
schema and feed endpoints. Metadata never overrides those live revisions. Existing
full-snapshot, exact diff/approval, bounded body, idempotency and asynchronous
indexing contracts are unchanged. A successful canonical commit does not mean
search reflection has completed; poll the documented state rather than replaying.

Sync HTTP failures are fixed `{"error":"..."}` envelopes with no-store (a
refused chunk also names `record`, `field` and `reason`; see the feed protocol):
400 for invalid requests/manifests/records/cursors, 413 for body/run bounds,
401 for invalid credentials, 403 for forbidden authority, 404 for foreign/hidden
targets or a disabled surface, 409 for changing authority/baselines/approval or
`SYNC-LOCKED`, 429 for the fail-closed OIDC burst limiter (per peer, and per
credential holder over the peer budget: client and user for the human control
routes, sync client for the service routes; every human control
route, `sync-authorizations` included, counts in one `sync-human` bucket, except
`publication-text`, which has its own `publication-text` bucket), and 503
`temporarily_unavailable` for unavailable authority/replay/burst stores. The
installation-bound domain operations also answer 429 `RATE-001` with
`Retry-After` for the workspace's domain verification limits, and 409
`public_mcp_domain_command_stale` when the binding or the named domain is no
longer as the command sent it (read the observation again). The schema upgrade
answers 409 `sync_schema_upgrade_runs_active` while the installation has an
active run. See the
linked contracts/OpenAPI for each route's exact code (including established
KMC error codes). Public MCP budget refusals are MCP protocol results, not this
control envelope. Do not retry semantic refusals blindly, reflect internal
exceptions, or use another credential namespace to bypass a refusal.

## Staged rollout gates

1. Keep Public scheduling/workers and all new sync switches off. Back up SQL,
   canonical events, managed ownership/receipts and publication/budget/domain
   state consistently. Preserve external billing/revocation and erasure ledgers
   outside the snapshot being restored.
2. Apply migrations before application enablement. The current baseline head is
   `f53_2810_sync_activity`, above `f52_2808_merge_feed_domains`. This issue adds
   no migration. Follow the existing [staged upgrade recipe](../deployment.md#how-each-account-was-admitted-issue-2803)
   for older databases; do not stamp, renumber, combine historically separated
   lock-sensitive upgrades, or assume the older f52 marker is the latest head.
3. Deploy compatible code to **every** API, maintenance, indexer and worker
   instance before advertising enabled profiles. Do not use a mixed fleet to
   negotiate on one instance and execute on an older instance. Verify a single
   migration head and run the tests below on an isolated database.
4. Register separate human and service clients/keys, trusted billing publisher
   and paid windows. Verify issuer/signing readiness and explicit human scopes.
   Enable prerequisites in order, then feeds/managed/activity and budget. Default
   settings remain off. A test canary needs real current owner consent and paid
   rights; the configuration document cannot grant either.
5. Verify one synthetic canary's canonical commit, search completion, private
   read, shared budget, stop/drain, key retirement and old-run refusal before
   allowing new consumers. Custom domains have their own later DNS/TLS/license
   gate. Check issuer/configuration on each instance and monitor actual refusals,
   lag, audit and budget—not just the release string.

### Record schema version 2 (`SYNC_SCHEMA_V2_ENABLED`, #3055)

Shipped off. With it off, discovery, every version-1 `/schema` answer and
every version-1 decision are byte-for-byte the previous release's; what a
client can see change is the optional `record` / `field` / `reason` keys of a
refused chunk and the OpenAPI document. No installation can reach version 2
and no `sync.schema.upgraded` event can appear.

Turn it on only when all of these hold:

1. every sync consumer of the deployment accepts both pairs and both `/schema`
   answers, and that consumer release can no longer be rolled back;
2. every API, maintenance, indexer and worker instance runs this release and
   has been restarted with the switch on (settings are read per process; during
   a rolling restart discovery alternates between the pairs and the upgrade
   route between 404 and its answer);
3. migration `f68_3055_sync_schema_v2` is applied.

Switch-on is one-way in practice: once an installation holds version 2, a
version-1-only consumer of it cannot sync again, and there is no downgrade
operation. Turning the switch off later is supported: installations already
on version 2 keep working in full, new installations get version 1 again and
the upgrade route answers 404. A code rollback to a release without version 2
is supported only while no installation has ever held version 2; after that,
forward fix only (an older binary answers 409 for an upgraded installation and
the f68 downgrade stops while a page or review feed exists). Version 1 has no
end date; an end would be announced at least one release ahead.

Each installation is upgraded once, by a human with `integrations:manage`,
after its sender has stopped scheduling and aborted or finished its open runs.

## Stop and rollback

For **one installation**, stop scheduling and use the authenticated
`/management/stop` then `/finalize` procedure. Retain `stopping` and the editing
lock until indexing/deletion postconditions are complete. A crash or partial
drain never authorizes hand editing. Unlink uses the same fence and preserves
the dedicated data/standard URL as described in the managed-data runbook.

For the **whole registered synchronization service**, use the central operator
control from the backend directory:

```sh
python -m src.cli.sync_service disable --service-id catalog-sync
python -m src.cli.sync_credentials disable --client-id catalog-worker
```

These commands serialize with admitted writers and advance durable generations.
After they commit, old credentials/runs cannot publish. Disable/revoke affected
keys with the existing credential CLI for a key compromise; do not use human
logout as a worker kill switch. Repeat for each separately registered service.
Stopping sync does not unpublish previously approved data: an incident requiring
publication shutdown must separately disable/revoke the public target and aliases.

Configuration changes apply per process, so a feature flag alone is **not** the
central atomic emergency stop. Quiesce workers and all instances, disable fresh
admission, then turn flags off. Keep current schema and ownership fences in
place for a code rollback. Never drop tables or clear ownership/cursors to make
an older binary run. Any DDL downgrade needs its own backed-up, approved plan;
f53 loses provenance receipts and lower revisions also lose canonical/managed/
domain/budget state. See the linked runbooks before considering a downgrade.

## Isolated backup recovery and reconciliation

Restore into an isolated environment with scheduler/worker egress, publication
routes/aliases and sync admission disabled. A database snapshot is **not** the
current revocation/billing/ownership truth. Never expose restored keys, grants,
jobs or public data before reconciling current external facts and the
[erasure restore procedure](erasure-runbook.md).

Disable restored services and clients before opening admission. Retire every
restored service signing key and replace private signing authority outside the
restored backup; similarly invalidate restored human/browser sessions, refresh
grants and OIDC signing authority through their existing controls. A generation
bump protects stored access tokens and prepared runs, but cannot alone stop a
new assertion signed with a still-trusted pre-restore private key. Do not merely
rename old key material or reset generations/replay receipts.

Reconcile current ownership/consent, paid rights, manual/stopping mode, domain
proof/license/certificate status, erasures and the workspace budget/period from
trusted records. Never manufacture remaining quota or rewind the seven-day
anchor because a snapshot was older. Keep uncertain targets unavailable. Recover
canonical SQL and asynchronous index postconditions without applying obsolete
source events over newer/manual data. Preserve receipts and use status polling
to distinguish a committed lost response from unfinished work.

Only after reconciliation, register fresh keys and explicitly reenable the
client/service with their existing CLI. Obtain fresh tokens and rebuild source
snapshots using **new** operation IDs from current configuration/schema/baseline.
Do not replay the restored queue, pinned candidates or approval receipts.
Reenable a canary first, then publication/aliases and scheduling after their
separate checks. Standard rollback/restore never automatically starts billing.

## Acceptance evidence and commands

Run with the repository's isolated test database/service environment and
`REQUIRE_TEST_DATABASE=1`; never point these tests at customer data:

```sh
pytest tests/api/test_sync_contract.py tests/integration/test_sync_contract_acceptance_db.py --no-cov -q
pytest tests/integration/test_oidc_provider_db.py tests/integration/test_oidc_workspace_access_db.py tests/api/test_oauth_authorization_code_flow.py tests/api/test_oauth_flow_binding.py tests/api/test_oauth_metadata_drift.py --no-cov -q
pytest tests/integration/test_sync_credentials_concurrency_db.py tests/integration/test_sync_feed_authority_db.py tests/integration/test_sync_feed_index_db.py tests/integration/test_sync_management_db.py tests/integration/test_sync_activity_authority_db.py tests/integration/test_public_usage_budget_db.py tests/integration/test_public_mcp_domain_alias_db.py --no-cov -q
pytest tests/integration/test_f53_2810_sync_activity.py tests/integration/test_f50_2807_merge_sync_heads.py tests/integration/test_f51_2808_managed_sync_migration.py tests/integration/test_f51_2809_public_mcp_domains.py --no-cov -q
```

The new recovery acceptance starts from durable pre-recovery records/queued runs
and exercises central quarantine, permanent stale-token/run refusal, retained
data/target/period, and fresh post-reconciliation publication. It tests the
**reconciliation boundary**, not physical `pg_dump`/`pg_restore`, DNS/certificate
providers, the real billing source or a deployed Public application. Those
physical restore and external/live consumer drills remain separate rollout gates;
unit/CI success must not be reported as a live rollout or production recovery.
