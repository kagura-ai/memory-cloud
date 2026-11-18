# Installation-bound service credentials

Issue #2806 adds service authentication for the fixed workspace-owned targets
created by #2802. It does not create installations, bypass owner consent, mint
human credentials, or implement feed/run/content APIs (tracked separately).

## Deployment and client registration

Apply `f45_2806_sync_credentials` before deploying/enabling the new grant.
Keep `SYNC_CREDENTIALS_ENABLED=false` until service registration and downstream
verification are complete. `OIDC_ENABLED` and `SYNC_INTEGRATIONS_ENABLED` must
also be enabled. Existing OIDC authorization-code/refresh behavior is retained;
discovery advertises the service grant, method, scope and RS256 only while the
new capability is enabled.

The #2802 service registration reserves a worker client ID separately from its
browser client. Browser and service operator registrations serialize allocation
of that ID across their separate tables. Service client registration cannot
select a different client ID or service binding.

From the backend directory, with the usual operator database configuration:

```console
python -m src.cli.sync_credentials register --service-id catalog-sync
python -m src.cli.sync_credentials add-key --client-id catalog-worker --public-jwk-file /run/config/worker-public.jwk
```

Only local, bounded, regular public JWK files are accepted. Symlinks, private
integers, remote key references and non-RSA/weak material are refused. The first
profile is RS256 with RSA 2048–4096 bits, `kid` of 1–128 ASCII letters/digits or
`._-`, `use=sig` and, if specified, `key_ops=["verify"]`. At most eight live keys
are allowed for one client. KMC stores canonical public material and its
fingerprint, never the worker's private key. Keep worker signing keys separate
from human OIDC signing keys and from other deployment environments.

## Frozen token request

Use the configured issuer's `/token` endpoint (`POST /oidc/token` on this
deployment), with `Content-Type: application/x-www-form-urlencoded`:

| Field | Exact value/profile |
| --- | --- |
| `grant_type` | `client_credentials` |
| `client_id` | Operator-registered worker identity |
| `client_assertion_type` | `urn:ietf:params:oauth:client-assertion-type:jwt-bearer` |
| `client_assertion` | Compact signed JWT described below |
| `scope` | `resource.sync` |
| `resource` | `<issuer>/sync/installations/<canonical installation UUID>` |

The resource URI is an identifier for one installation, rather than a lookup
of arbitrary URLs or resources. Query parameters, duplicate/unknown form keys,
`Authorization` client authentication, `client_secret`, user refresh tokens and
cookie fallback are refused. The existing 8 KiB/32-field parser and fail-closed
Redis anonymous burst gate bound protocol work before cryptography/DB queries.
The service metadata route uses the same per-peer burst policy, with one key
shared across installation IDs; these are not public-MCP usage charges. Over
the peer budget, a request with a service token this server issued is counted
for the token's credential client instead, and an unknown token keeps the 429
(see [OIDC provider](oidc-provider.md#protocol-burst-limits)). The
`client_credentials` grant itself has no such credential and stays per peer.

The assertion header has `alg=RS256` and the registered `kid`. Recommend
`typ=client-authentication+jwt`; omitted `typ` or `JWT` remain accepted for
interoperability. Other explicit types, JOSE critical/extensions, embedded or
remote keys, and duplicate JSON members are rejected.

| Claim | Constraint |
| --- | --- |
| `iss`, `sub` | Both equal the registered client ID |
| `aud` | Exactly the configured authorization-server issuer string |
| `iat`, `exp` | Integers; `iat <= now < exp`, `0 < exp-iat <= 60` seconds |
| `nbf` | Optional integer, never later than now |
| `jti` | Required unique 16–128 ASCII letters/digits or `._-` |

No clock-skew extension or expiry grace is applied. Workers need synchronized
clocks. Audience arrays and extra claims are not part of this initial profile.
No audience or key source is derived from Host, forwarded headers, redirects or
an untrusted metadata document.

**Audience update:** the original draft used the token endpoint URL. The frozen
contract instead uses the issuer as the sole audience, following the
[RFC 10070 §4 update](https://www.rfc-editor.org/rfc/rfc10070.html#section-4)
to RFC 7523. The token endpoint remains unchanged. The resource identifier
separately restricts the recipient/target of the issued access credential, as
in [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707.html).

### Replay and outages

A verified assertion consumes a PostgreSQL receipt keyed by client ID and a
SHA-256 JTI digest, atomically with a unique index. The short receipt transaction
commits independently before paid/installation admission. The plain-read auth
session releases its connection before acquiring the receipt connection, so
concurrent requests cannot exhaust the pool by each waiting for a second
connection. No process-local or Redis replay fallback exists.

An accepted assertion cannot be retried after paid/target refusal, cancellation
of issuance, response loss, or a rolled-back token transaction. Generate a new
JTI/assertion. Malformed requests rejected before client authentication do not
consume an assertion. Concurrent presentations have at most one winner. A
replay-store or burst-store outage fails closed with a fixed 503; no verifier is
issued without the durable receipt. Receipts must survive until assertion
expiration; pruning expired receipts cannot revive the expired assertion.

### Token and error responses

Success returns exactly `access_token`, `token_type=Bearer`, `expires_in=300`,
and `scope=resource.sync`. There is no ID token, human subject or refresh token.
Access tokens are random `kmc_sync_at_` values with 256 bits of entropy. Only
their SHA-256 verifiers and authority bindings are stored. Keep plaintext tokens
only in worker memory, and obtain a fresh token when needed.

Token errors use fixed `invalid_request`, `invalid_client`, `invalid_scope`,
`invalid_target`, `access_denied`, `unsupported_grant_type`, or
`temporarily_unavailable` values. Current paid rights can expire before the
token's five-minute lifetime ends: every operation rechecks them. No error,
audit or logger output contains a signed assertion, raw JTI, access credential,
private material or database/JOSE exception text. Every response is no-store.

## Mandatory consumer transaction boundary

`services.sync_credentials.sync_token_boundary` is the only admission boundary
for consumers of these credentials. It requires an explicit approved operation
(`schema:read`, `records:write` or `results:read`). It never returns a user ID or
feeds a service principal into general REST/MCP/API-key/Resource Token auth.

The delivered `GET /api/v1/sync/installations/{installation_id}` consumes
`results:read` and returns only its fixed operational target IDs, generation and
approved operations. Product/FAQ staged runs and human data projection are
documented in [staged feed synchronization](staged-feed-sync.md). Each worker
operation uses the same boundary around its actual content changes; a prior
metadata inspection is not authority.

The boundary owns the transaction. Only plain SELECTs may precede the paid guard;
pending/flushed writes or earlier row locks are refused without rolling back
the caller's transaction. After the workspace paid fence, shared locks reload
service, client, key, installation, and the exact Resource/Context/publication
binding, then the access-token row. Secondary authority locks use NOWAIT to
avoid waiting under a workspace lock on opposing legacy lock orders; changing
authority produces a retryable fixed 503, not permission to continue.

The current enabled/state values, service/client/installation generations,
key identity/fingerprint, approved operations, immutable UUID/slug/workspace
binding, publication visibility, token audience/scope and finite deadlines must
all still match. A synchronous final hook is retained through the paid guard's
flush/recheck/commit using an outer `AsyncExitStack`. Token/assertion and paid
expiry after the last async work still roll back the write. Early/nested commits
are forbidden. Failure/cancellation rolls back mutations and releases the hooks
and locks. Initial issuance follows the same paid/target boundary.

The workspace-owned installation survives owner transfer; the former approver's
login/refresh grant is not a continuing dependency. A paused/revoked installation
or missing/replaced target is never silently repaired, resumed or rebound.

## Rotation, revocation and cleanup

1. Add a new public key under a fresh `kid` while keeping the old key live.
2. Switch workers to the new private key and confirm usage.
3. Explicitly retire the old key:

```console
python -m src.cli.sync_credentials revoke-key --client-id catalog-worker --kid previous
python -m src.cli.sync_credentials disable --client-id catalog-worker
python -m src.cli.sync_credentials enable --client-id catalog-worker
python -m src.cli.sync_service disable --service-id catalog-sync
python -m src.cli.sync_credentials prune
```

Revocation also rejects access tokens issued with that key. It serializes with
admitted writes: a write may commit before the revocation commits, but an old
credential cannot write after a successful revocation. Retired key IDs cannot
be rebound/reactivated. Adding a rotation key does not kill other live keys.
The same public material cannot be registered under a second key ID for that
client, including after retirement; changing only `kid` cannot revive a key.
Client and service disable/enable switches advance their generations; old
access tokens never revive after reenabling. Existing installation unlink
advances installation generation under the workspace fence. Request fresh
assertions/tokens for retryable changing-authority errors.

Revoking a human OIDC login/client is not the service emergency switch. Use the
service/client/key controls above to stop worker authority independently of
the original approver's login lifecycle.

`prune` removes only expired tokens and assertion receipts. Register/rotation/
revocation actions and issuance use explicit operator/service audit actors,
not an impersonated owner. New tables are operational auth state excluded from
memory exports. Workspace/installation deletion cascades token verifiers;
service key/client records are global service infrastructure, not user grants.

For rollback, turn the credential capability off before reverting the app/DDL.
The credential revision itself removes only credential/client/key/replay tables;
existing workspace, installation, Resource events, Memories and publication
identities remain. Reaching it from the combined head also removes, first, the
five managed-feed tables and the custom domain table `public_mcp_domains` with
every binding, then the later
staged-feed canonical/staging tables and public-budget rows: follow the backup
and feature-shutdown precautions below before doing so. Re-upgrade
requires explicit operator client/public-key registration; it does not restore
old token or replay authority. Production enablement and downstream rollout are
separate from merging this implementation.

Migration `f47_2819_merge_sync_credentials` reconciles the credential revision
with the parallel notice/admission history. `alembic upgrade
f47_2819_merge_sync_credentials` applies any missing branch before that no-DDL
merge node. For the current combined checkout, follow the staged upgrade recipe
in [deployment.md](../deployment.md#how-each-account-was-admitted-issue-2803)
instead of applying all remaining revisions in one transaction. Existing
revision IDs and data remain intact. Downgrading
only the merge node restores both parent markers and keeps both schemas; removing
the credential tables is a separate downgrade governed by the precautions above:
`alembic downgrade f45_2806_sync_credentials@f44_2802_sync_installations`
runs only the downgrades of the revisions above the merge node (from the
combined head: `f52_2808_merge_feed_domains`, `f51_2808_managed_sync`,
`f51_2809_public_mcp_domains`, `f50_2807_merge_sync_heads`,
`f48_2807_sync_feed_runs`,
`f49_2817_clear_allowlist_adders` and `f48_2805_public_usage_budgets`), the
merge node's and the credential revision's, and leaves
`f46_2803_admission_record` applied. Do **not** use a plain
`alembic downgrade f44_2802_sync_installations` for that: from the merged head it
reverts both branches, so it also drops the admission records (for good) and
the notice plans, or stops at the notice-plan downgrade guard. To remove only
the admission record, use `alembic downgrade f45_2800_mcp_notice_plans`
(it, too, first reverts `f52_2808`, both `f51` revisions, the combined-head
merge `f50_2807` and both later branches).
To undo just the merge, name either immediate parent explicitly; relative `-1`
is ambiguous at a merge point. For example, from this merge revision use
`make migrate-down revision=f46_2803_admission_record` (at the combined head,
`f52_2808`, `f51_2808`, `f51_2809`, `f50_2807`, `f48_2807`, `f49_2817` and
`f48_2805` are reverted first); both parent schemas
remain. Re-upgrade in separate steps: `alembic upgrade
f47_2819_merge_sync_credentials`, then `alembic upgrade
f49_2817_clear_allowlist_adders`, then `alembic upgrade head`.

From the combined head, a downgrade below the shared `f47` ancestor, including
these per-branch operations, first removes the five staged-feed tables. Stop
new runs, drain committed indexing and back up feed tables before doing so;
see [staged-feed rollback](staged-feed-sync.md#retention-and-rollback). Merely
unmerging `f50_2807` to either explicit parent leaves both parents applied.

From v0.164.0, `f51_2808_managed_sync` and `f51_2809_public_mcp_domains` both
follow the merge node `f50_2807_merge_sync_heads`, and the no-DDL merge
revision `f52_2808_merge_feed_domains` joins them as the single head. A plain
downgrade to `f50_2807` and every downgrade below it, these per-branch ones
and unmerging `f50_2807` included, first unmerges `f52_2808` and reverts both
`f51` revisions. `f51_2808` drops the five managed-feed tables
(`sync_managed_targets`, `sync_resume_candidates`, `sync_resume_previews`,
`sync_mode_operations` and `sync_manual_retirements`): the ownership rows,
operation receipts, resume snapshots and retirement proofs are lost, and the
v0.164.0 application cannot write content or list Contexts without
`sync_managed_targets`, so stop it first (see
[Feed ownership and safe manual operation](feed-managed-sync.md)). `f51_2809`
drops the table `public_mcp_domains` with every binding: every custom domain alias stops being served and every claim, proof state and
cooldown is lost. Take the alias routing out of the proxy first, then switch
`MCP_PUBLIC_DOMAINS_ENABLED` off, before such a downgrade (see
[Public MCP custom domains](../public-mcp-domains.md), "Stopping and rolling
back").

From v0.163.0, every downgrade below `f48_2805_public_usage_budgets`, these
per-branch ones included, first runs `f49_2817` (no-op) and `f48_2805`, which
drops the table `public_usage_budgets`. Turn `MCP_PUBLIC_USAGE_BUDGET_ENABLED`
off before it: with the switch on and no table, public `tools/call` is refused
(`temporarily_unavailable`), counted anonymous REST searches answer `503` and
the public MCP owner routes answer `503 DB-002`. The table comes back only with
`alembic upgrade head`, which also re-applies what the downgrade removed (see
[Public usage budget](../public-usage-budget.md)). After a downgrade that
reverted `f46_2803_admission_record` (`alembic downgrade
f45_2800_mcp_notice_plans`, or a plain downgrade to `f44_2802_sync_installations`
or lower), run `alembic upgrade f47_2819_merge_sync_credentials` first, then
`alembic upgrade f49_2817_clear_allowlist_adders`, then `alembic upgrade head`,
so that `f46_2803` does not share a transaction with `f48_2805`, nor
`f48_2805` with `f48_2807` (see "How each account was admitted" in
[deployment.md](../deployment.md)).
