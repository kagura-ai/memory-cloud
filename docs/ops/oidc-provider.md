# Isolated OIDC provider

The optional provider under `/oidc` authenticates pre-registered external web
applications. It is separate from the MCP OAuth server, dynamic client
registration, memory API keys, resource tokens and billing handoff credentials.
It does not grant a workspace role simply because someone has signed in.

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

## Protocol burst limits

`OIDC_RATE_LIMIT_PER_MINUTE` defaults to 120 requests per trusted peer address
and endpoint. Authorization, token exchange, revocation and authenticated
UserInfo work are protected before database or signing operations. Discovery
and JWKS remain available independently of that counter.

An exhausted budget returns 429 with `Retry-After`; an unavailable Redis
counter returns 503 instead of allowing unlimited authentication attempts.
The peer address comes from ASGI, so configure Uvicorn to trust only the actual
reverse proxy. Raw client-supplied forwarding headers do not choose the key.
Size the limit for a web application's shared server egress address as well
as direct browser requests. Neither credentials nor raw peer addresses are
stored in the counter keys.

## Lifetimes and revocation

Authorization codes expire after 60 seconds and can be exchanged once. ID
tokens and access tokens last at most five minutes. Refresh tokens rotate and
remain within the original eight-hour grant deadline; rotation cannot extend
that deadline. Access tokens are opaque and are accepted only by the isolated
identity resource. The issuer's account ID is the stable `sub`, regardless of
the email or upstream sign-in provider shown to the user.

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

The signing configuration is reloaded for each operation. Publish files with
an atomic replacement; writing in place can temporarily produce a safe 503.
Back up private material through the secret-management process, separate from
public JWKS and database exports. After compromise, rotate the key and revoke
affected clients/grants; removing a public JWKS entry alone does not revoke a
client's already cached ID tokens.

## Verify before enabling traffic

Check Discovery's exact issuer and endpoint URLs, public-only JWKS, a complete
HTTPS Code Flow, client authentication, S256 enforcement, UserInfo subject
matching, code/refresh reuse rejection, fixed expiry, client revocation, and
password-reset revocation. Also verify that the new tokens are refused by
memory and MCP APIs and that existing MCP clients still work. Inspect API and
ingress logs for credential leaks without recording test payloads or live
secrets in operational evidence.
