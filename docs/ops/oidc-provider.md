# Isolated OIDC provider

The optional provider under `/oidc` authenticates pre-registered external web
applications. It is separate from the MCP OAuth server, dynamic client
registration, memory API keys, resource tokens and billing handoff credentials.
It does not grant a workspace role simply because someone has signed in.

Clients with `workspaces:read` can use the separate
[workspace access inspection API](oidc-workspace-access.md) to resolve current
membership and effective permissions after login.

## Enable and configure

The default is `OIDC_ENABLED=false`. Configure all of the following before
enabling the provider:

| Setting | Meaning |
| --- | --- |
| `OIDC_ENABLED` | Explicit opt-in. Missing signing configuration fails closed. |
| `OIDC_ISSUER` | Fixed HTTPS origin followed by `/oidc`, for example `https://identity.example.test/oidc`. No trailing slash, query, fragment or credentials. |
| `OIDC_SIGNING_KEYS_FILE` | Operator-mounted private JWK set or a single RSA private PEM file. |
| `OIDC_ACTIVE_SIGNING_KID` | The unique key ID of the current private signing key. |

Use an RSA key of at least 2048 bits. RS256 is the only ID-token algorithm.
The API never creates a replacement signing key automatically. Do not reuse
another service's signing or encryption key. Restrict the file to the API
service account (`0400` or `0600` on POSIX; equivalent ACLs on Windows), keep it
outside source control, and mount its containing directory read-only. A symlink, non-regular file,
oversized file or group/world-readable POSIX file is refused.

For Docker, bind-mount the directory so an atomic file replacement becomes
visible inside the container. A single-file bind mount stays attached to the
old inode after a host-side replacement; recreate every API container using
that mount to load the replacement file.

On the single-server layout (`terraform/single-server/docker-compose.prod.yml`
and `docker-compose.app.yml`) the compose files carry that mount: the host
directory `/opt/kagura-memory/oidc` appears read-only at `/run/kagura/oidc` in
`api-blue` and `api-green`, so `OIDC_SIGNING_KEYS_FILE` is a path under
`/run/kagura/oidc` ([#2885](https://github.com/kagura-ai/memory-cloud/issues/2885)).

- **Containers created before the mount was added do not have it.** Run
  `deploy.sh` once with a release that has the mount before placing the key
  and setting `OIDC_ENABLED=true`. After that first deploy only the new color
  has it: `deploy.sh --rollback` goes back to the old container, where
  `/run/kagura/oidc` does not exist and `/oidc/*` answers `503`. A second
  deploy gives both colors the mount.
- **Owner and mode.** The API runs as uid 1000 inside the container, and a
  bind mount keeps the host's owner and mode. The key file must be owned by
  uid 1000 on the host with mode `0400` or `0600`, and the directory must let
  uid 1000 enter it (for example `root:root` `0755`). The host account with
  uid 1000, often the first login account of the VM, can then read the key:
  treat that account as you treat the one that can read `.env.prod`.
- **Replacing the file** keeps working only when the new file has the same
  owner and mode before the rename, for example
  `sudo install -o 1000 -g 1000 -m 0400 new-keys.json /opt/kagura-memory/oidc/.signing-keys.json.new`
  followed by `sudo mv` over the live name. A file left owned by root or by
  the operator is unreadable for the API, and `/oidc/*` answers `503` at once.
  The same happens after a recursive `chown` of `/opt/kagura-memory` (the
  set-up step in `terraform/single-server/README.md`): restore uid 1000 on the
  key file afterwards.
- **Keep only the live file in the directory.** Everything in it is visible
  to both API containers, so remove staged copies, backups and retired keys.
- **A missing host directory** is created by Docker at `compose up`, empty and
  owned by root. Nothing reads it while `OIDC_ENABLED` is off, so a deployment
  that does not use the provider needs no preparation.
- **A missing or refused key file** with `OIDC_ENABLED=true` answers `503` on
  `/oidc/*`. The keys are read when they are first needed, not at start-up, so the
  containers start and the deploy's readiness check stays green. Clients
  cannot sign in or refresh a token meanwhile, so everything that needs an
  OIDC token (the sync owner controls, for example) stops within the token
  lifetime of five minutes.

A JWK set has a `keys` array. Each key needs a distinct `kid`; the active key
needs RSA private material. Previous public RSA keys may remain in the set for
verification overlap. Optional `alg` and `use` must be `RS256` and `sig`.
Only `kty`, `n`, `e`, `kid`, `alg` and `use` are published through JWKS; private
integers and arbitrary operator metadata are never forwarded. A single PEM
file uses the configured active key ID; use a JWK set for overlapping rotation.

The production Caddy template routes `/oidc/*` to the API. Apply the same route
when using another ingress. The issuer comes from configuration, never from an
untrusted request Host header. Keep discovery and endpoint URLs on that exact
issuer. Existing root MCP discovery and OAuth endpoints remain independent.

## Register a confidential web client

Apply the database migration, then run the operator CLI in the backend/API
environment against its intended database. There is no public registration
endpoint for these clients. The CLI uses the existing production secret guard.

Choose a random client secret, store the same value in the external
application's server-side secret store, and provide it through a protected
file or the hidden terminal prompt. The value must contain 32–72 UTF-8 bytes
(bcrypt's supported input range). Never pass it as an argument, print it, put
it in a browser, or commit it. File input permits one final line ending but
preserves spaces in the secret.

```sh
cd backend
python -m src.cli.oidc_client register \
  --client-id external-web \
  --name "External web application" \
  --redirect-uri https://app.example.test/auth/callback \
  --secret-file /run/secrets/external-web-client
```

Omit `--secret-file` only in an interactive terminal; the CLI prompts twice
without echo. It stores only a bcrypt verifier and prints non-secret metadata
after a successful commit. A duplicate client ID is refused rather than
overwriting a registered redirect or secret.

Redirect URIs must be exact HTTPS URLs, with no wildcard, credentials or
fragment. Repeat `--redirect-uri` for multiple approved callbacks. Development
uses a test HTTPS origin; it does not relax this production contract.

The default registered scopes are `openid profile workspaces:read`. Add an
explicit `--scope "openid profile workspaces:read integrations:manage"` only
when the application requires integration approval. These account/integration
scope names do not confer memory or MCP permissions. The initial provider's
protected resource is `/oidc/userinfo`; account/permission APIs are separate
deliverables. Neither an unimplemented API nor an unknown scope becomes
available through a scope fallback.

Clients support confidential `client_secret_basic` or `client_secret_post`
authentication at the token endpoint. Authorization Code flow additionally
requires PKCE S256 and a nonce for every client. Register the issuer with the
client library and use its standard signature, issuer, audience, nonce, state
and PKCE validation. Do not use an ID token as an API access token.

### Consent page language

The consent page is shown in English or Japanese (the values discovery lists
as `ui_locales_supported`). The language is chosen in this order:

1. the authorization request's `ui_locales` parameter (OIDC Core 3.1.2.1): a
   space-separated list in preference order; the first tag whose primary
   subtag is supported wins, ignoring case (`ja-JP` and `JA` both give
   Japanese);
2. the signed-in account's saved language;
3. the browser's `Accept-Language` header (q-values respected);
4. English.

Unsupported or malformed tags, a `ui_locales` longer than 255 characters and
an `Accept-Language` header longer than 1024 characters are skipped without
an error and the next source decides; the authorization request's overall
size limit and the one-value-per-parameter rule still apply. An account's
saved language is `en` unless the user chose another, so a relying party that
wants the Japanese page for such an account passes `ui_locales=ja`;
`Accept-Language` decides only when the account has no supported saved
language. `ui_locales` is optional and covered by the consent form's binding
to the request like every other authorization parameter.

## Protocol burst limits

`OIDC_RATE_LIMIT_PER_MINUTE` (default 120) is applied per minute in two
buckets, each per endpoint:

1. **Per peer address.** Every authorization, token, revocation and UserInfo
   request (and the workspace, sync control and sync service routes that take
   these credentials) is counted for its peer address before any validation,
   database or signing work. Under this budget nothing else is checked.
2. **Per credential holder, over the peer budget.** A request over the
   per-peer budget is still admitted when it presents a credential this
   server issued and recognises with one indexed lookup, without any
   client-secret or signature work: an authorization code or refresh token on
   `/oidc/token`, the token being revoked on `/oidc/revoke`, an OIDC access
   token on UserInfo, the workspace routes, the sync control routes and the
   setup handoff, or a sync service token on the sync service routes. It is
   then counted in a bucket for that credential's holder, with the same
   limit. An authorization code, refresh token or access token is held by
   the client and the user it was issued to together, so each signed-in user
   of a client has a bucket of their own; a sync service token is held by its
   sync client. On `/oidc/authorize` a browser that is already signed in is
   counted for its account; the session cookie was already resolved for the
   request. Recognition only decides admission: the request then goes
   through the usual checks and gets the same answer it would get within the
   budget.

A request over the per-peer budget without such a credential (none, an
unknown one, or a `client_credentials` grant, whose signed assertion is new
for every request) gets 429 with `Retry-After`, before any client
authentication. A holder over its own bucket gets the same 429; other users
of the same client, and other clients, are not affected. An unavailable Redis counter, for either bucket, returns 503
instead of allowing unlimited authentication attempts. Discovery and JWKS
remain available independently of these counters. Neither credentials, client
IDs nor raw peer addresses are stored in the counter keys.

### Peer address behind a reverse proxy or CDN

The peer address comes from ASGI. Raw client-supplied forwarding headers do
not choose the key; Uvicorn replaces the peer address only for requests from
the hosts it trusts. Behind a reverse proxy, every caller otherwise shares the
proxy's address and so one per-peer budget. To key the first bucket on the
caller's address:

1. **Application server.** Start Uvicorn with
   `--forwarded-allow-ips=<proxy address or network>` (or set
   `FORWARDED_ALLOW_IPS`), for example `FORWARDED_ALLOW_IPS=192.0.2.10`.
   List only the reverse proxy, never `*` while anything else can reach the
   API port. Uvicorn then takes the client address from `X-Forwarded-For`,
   skipping the trusted hosts from the right.
2. **Reverse proxy.** The proxy must send the caller's address in
   `X-Forwarded-For`. Directly in front of the API, Caddy's `reverse_proxy`
   does this by default.
3. **CDN in front of the proxy.** The proxy's own peer is then a CDN edge
   address that unrelated visitors share. Configure the proxy to accept the
   CDN's client-address header only from the CDN's published address ranges,
   and to forward exactly that address. For Caddy (the ranges and the header
   name come from your CDN's documentation, for example `CF-Connecting-IP`):

   ```caddyfile
   {
   	servers {
   		trusted_proxies static 198.51.100.0/24 203.0.113.0/24
   		client_ip_headers CF-Connecting-IP
   	}
   }

   identity.example.com {
   	reverse_proxy api:8080 {
   		header_up X-Forwarded-For {client_ip}
   	}
   }
   ```

   Requests that do not come from those ranges keep their own peer address,
   so the header cannot be supplied by a caller that reaches the proxy
   directly. Keep the range list current when the CDN publishes changes.
   The `header_up` line is needed as well as the two global lines: for a
   request from a trusted proxy, Caddy's default `X-Forwarded-For` keeps the
   incoming list and appends its direct peer, the CDN edge, and Uvicorn
   would take that edge address as the caller.

   In the single-server template's Caddyfile, the two lines go into its
   existing global `servers` block, and `header_up X-Forwarded-For {client_ip}`
   goes into every `reverse_proxy ${API_UPSTREAM}:8080` (the `/oidc/*`,
   `/api/*`, `/oauth/*`, `/mcp*` and `/.well-known/*` handlers and the other
   API handlers, including the public MCP domain block when it is enabled).
   A one-line `reverse_proxy ${API_UPSTREAM}:8080` becomes a block holding
   that line, as in the example above. The API containers reach Caddy over a
   Compose network: find that network's subnet with `docker network inspect`
   and use it for `FORWARDED_ALLOW_IPS` in step 1.
4. **Check.** Send a request through the CDN and confirm in the API's access
   log that it carries the caller's address, not the proxy's or the CDN's.

Without these steps, requests that carry a credential this server issued, or
come from a signed-in browser, are still counted per holder when the shared
per-peer budget is spent. Other requests share the per-peer budget and get the
429 while it is spent: `client_credentials` token requests (so a sync service
cannot renew its five-minute service token) and `/oidc/authorize` requests
from browsers that are not signed in. Deployments that use sync service
credentials, or whose users start sign-in through this provider, should
complete the steps above. Size the limit for a web application's shared server
egress address as well as direct browser requests.

## Lifetimes and revocation

Authorization codes expire after 60 seconds and can be exchanged once. ID
tokens and access tokens last at most five minutes. Refresh tokens rotate and
remain within the original eight-hour grant deadline; rotation cannot extend
that deadline. Access tokens are opaque and are accepted only by the isolated
identity resource. The issuer's account ID is the stable `sub`, regardless of
the email or upstream sign-in provider shown to the user.

A credential used twice ends its grant. When a client presents an
authorization code that was already exchanged, the token endpoint answers
`invalid_grant` and revokes the grant behind the code, so the access and
refresh tokens of the first exchange stop working as well. Presenting an
already rotated refresh token does the same. A client that retries a code
exchange after a lost response, or sends the same exchange twice at once,
therefore must restart the authorization flow. A code that expired unused, an
unknown code, or a code presented by a different client revokes nothing.

An operator can revoke an external client:

```sh
cd backend
python -m src.cli.oidc_client revoke --client-id external-web
```

Revocation is checked against current server-side client state. It refuses new
authorizations, code exchanges, refreshes and UserInfo access for that client.
A previously issued signed ID token can still be cryptographically valid until
its short expiry; applications must not treat it as current workspace
authorization. Password-reset and account-erasure flows also revoke their
identity grants. The external application's own local session and global
browser sign-out are separate concerns.

To replace a client secret, provision a distinct client ID with the intended
callbacks and scope, switch the external application's confidential
configuration, verify login, then revoke the previous client. This procedure
does not silently alter another application's registration.

## Signing-key rotation

1. Generate a new private RSA key and distinct `kid` through the operator's
   secure tooling. Stage a JWK set containing both old and new keys, while
   retaining the old active private key.
2. Atomically replace the file inside the mounted directory. If using a
   single-file bind mount, recreate the API containers after replacing the
   host file. Confirm that JWKS publishes both public keys before switching
   any instance's active key ID.
3. Change `OIDC_ACTIVE_SIGNING_KID` to the new ID and roll the API instances.
   Verify that newly issued ID tokens carry the new `kid` and that old tokens
   remain verifiable.
4. After every instance has switched, remove the old private parameters. Keep
   its public key through the maximum old-token lifetime plus the deployment's
   clock-skew/cache overlap. Remove the old public key only after that window.

Each API process keeps the signing keys it parsed and re-reads the file when it
changes: every operation compares the file's device, inode, modification and
change time, size and mode, and the `OIDC_ISSUER`, `OIDC_ACTIVE_SIGNING_KID` and
`OIDC_SIGNING_KEYS_FILE` settings, with the ones the keys were parsed from. An
atomically replaced file therefore takes effect on the next request. A
replacement that cannot be read or validated answers 503; the keys parsed
before it are not used. Publish files with an atomic replacement; writing in
place can temporarily produce a safe 503.
Back up private material through the secret-management process, separate from
public JWKS and database exports. After compromise, rotate the key and revoke
affected clients/grants; removing a public JWKS entry alone does not revoke a
client's already cached ID tokens.

## Verify before enabling traffic

Check Discovery's exact issuer and endpoint URLs, public-only JWKS, a complete
HTTPS Code Flow, client authentication, S256 enforcement, UserInfo subject
matching, code/refresh reuse rejection with grant revocation, fixed expiry,
client revocation, and password-reset revocation. Also verify that the new
tokens are refused by memory and MCP APIs and that existing MCP clients still
work. Inspect API and ingress logs for credential leaks without recording
test payloads or live secrets in operational evidence.
