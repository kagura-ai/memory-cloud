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
shared across installation IDs; these are not public-MCP usage charges.

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
Downgrade removes only credential/client/key/replay tables; existing workspace,
installation, canonical content and publication identities remain. Re-upgrade
requires explicit operator client/public-key registration; it does not restore
old token or replay authority. Production enablement and downstream rollout are
separate from merging this implementation.

Migration `f47_2819_merge_sync_credentials` reconciles the credential revision
with the parallel notice/admission history. Use the ordinary `alembic upgrade
head` command from either prior head: Alembic applies the missing branch before
the no-DDL merge node. Existing revision IDs and data remain intact. Downgrading
only the merge node restores both parent markers and keeps both schemas; removing
the credential tables is a separate downgrade governed by the precautions above:
`alembic downgrade f45_2806_sync_credentials@f44_2802_sync_installations`
runs only the merge node's and the credential revision's downgrades and leaves
`f46_2803_admission_record` applied. Do **not** use a plain
`alembic downgrade f44_2802_sync_installations` for that: from the merged head it
reverts both branches, so it also drops the admission records (for good) and
the notice plans, or stops at the notice-plan downgrade guard. To remove only
the admission record, use `alembic downgrade f45_2800_mcp_notice_plans`.
To undo just the merge, name either immediate parent explicitly; relative `-1`
is ambiguous at a merge point. For example, from this merge revision use
`make migrate-down revision=f46_2803_admission_record`; both parent schemas
remain, and `alembic upgrade head` rejoins them.
