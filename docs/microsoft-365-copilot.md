# Microsoft 365 Copilot: the `microsoft` Tool Profile and OAuth Client

This page describes how the Remote MCP server is connected to Microsoft 365 Copilot as a plugin or connector with OAuth 2.0 ([#2778](https://github.com/kagura-ai/memory-cloud/issues/2778)): the MCP URL to register, the OAuth client to create and who should own it, the Teams developer portal registration, which tools Copilot asks the user to confirm, and the validation targets with how to measure them. It describes the code; it makes no compliance claim. The publisher-side steps (Partner Center enrollment, verification) happen outside this repository.

## 1. MCP URL

```
https://example.com/mcp?profile=microsoft
```

`?profile=microsoft` is an alias of [`?profile=google`](google-connected-apps.md): the same tools, descriptions, annotations, server instructions and replies, from one implementation. Copilot runs only `readOnlyHint: true` tools without asking, asks before every other tool, and its Researcher agent uses read-only tools only; on this view `recall` is read-only (`readOnlyHint: true`, no learning writes), so search works in every Copilot experience. What the view changes compared with the default surface is listed in [Google Connected Apps › What the profile changes](google-connected-apps.md#2-what-the-google-profile-changes) and [MCP Tools › The `google` profile](mcp-tools.md#the-google-profile).

- Use the bare `/mcp` form for a plugin that many users share. Each user's calls run in their own workspace (the one selected in the web UI). The pinned form `/mcp/w/<workspace_id>?profile=microsoft` sends every user to that one workspace, and users who are not members of it get `403`.
- `?context=<context_id>` can be added for a single-tenant deployment whose users share one context. Do not add `?tools=`: an explicit allowlist wins over `profile`, and the URL is then no longer this view.

## 2. The OAuth client

Microsoft's dynamic client registration needs a client secret ("DCR without a client secret isn't supported yet"), and this server's DCR registers public clients only, so the Microsoft client is a **static confidential client**. Create it as a custom OAuth app on the credentials page, or with the API:

```http
POST /api/v1/oauth/clients
Content-Type: application/json

{
  "client_name": "Microsoft 365 Copilot",
  "redirect_uris": ["https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"]
}
```

The response carries `client_id` and `client_secret`. The secret is shown once (and stays visible for 10 minutes), so copy it into the Teams developer portal (below) straight away.

- **Redirect URI.** Exactly `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`, the one URI Microsoft uses for every plugin. Matching is exact: another path, a trailing slash, a query, another host or `http` is refused at `/authorize`.
- **Client authentication.** The client is created for `client_secret_post` (the secret in the token request body). `client_secret_basic` (HTTP Basic) works too when the client is created with `"token_endpoint_auth_method": "client_secret_basic"` (API only). The server accepts only the method the client was created with, so if the token exchange fails with `invalid_client` although the secret is right, create a client with the other method and update the registration.
- **Who can sign in.** Any user of any workspace. The authorize and token endpoints check only the client, its secret and the redirect URI; the token is the signing-in user's, and their tool calls run with their own roles in their own workspaces. The client's own account and workspace grant nothing.

### Who should own the client

The client belongs to the account that created it (`owner_id`) and to that account's workspace at the time (`workspace_id`); only that account can see, edit or delete it. Create it from an **operator account** (a shared, role-owned account), not a personal one:

- Deleting the owning account, deleting that workspace, or removing the account from that workspace (which deletes its credentials there) deletes the client, and every Copilot user's connection stops working.
- Regenerating the secret stops token refreshes for every user until the new secret is in the Teams registration.
- Changing the redirect URI to anything else breaks sign-in.

### Refresh tokens

Every refresh returns a new refresh token, and the previous one stops working (refresh-token rotation, RFC 6819 §5.2.2.3). Microsoft's token store keeps the newest token; Microsoft's documentation does not forbid rotation. (Google's connected-apps program does, which is why [#2773](https://github.com/kagura-ai/memory-cloud/issues/2773) keeps refresh tokens for Google's client only; see [MCP Client Setup › Google connected apps](mcp-clients.md#google-connected-apps).) A user who signs out of the agent in Copilot clears Microsoft's stored token; the grant itself is revoked from the user's Settings like any other app's.

## 3. Teams developer portal: OAuth client registration

In the Teams developer portal, **Tools → OAuth client registration → New OAuth client registration** (Microsoft's guide: [Configure OAuth 2.0 authentication](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-oauth)):

| Field | Value |
|---|---|
| Registration name | Any name, e.g. `Kagura Memory` |
| Base URL | The MCP URL of the plugin manifest: `https://example.com/mcp?profile=microsoft` |
| Restrict usage by org | **Any Microsoft 365 organization** for a listing; **My organization only** while testing in one tenant |
| Restrict usage by app | **Any Teams app** (Microsoft: a registration bound to one Teams app ID makes every MCP tool call fail with `404`) |
| Client ID | `client_id` from step 2 |
| Client secret | `client_secret` from step 2 |
| Authorization endpoint | `https://example.com/api/v1/oauth/authorize` |
| Token endpoint | `https://example.com/api/v1/oauth/token` |
| Refresh endpoint | `https://example.com/api/v1/oauth/token` (the same endpoint) |
| Scope | `memory:read memory:write memory:delete offline_access` |
| Enable PKCE | On (the server supports `S256` only) |

The endpoints are the ones `/.well-known/oauth-authorization-server` publishes. Saving the registration gives an **OAuth client registration ID** (the auth config ID); the plugin manifest names it in its runtime authentication object:

```json
"auth": {
  "type": "OAuthPluginVault",
  "reference_id": "<OAuth client registration ID>"
}
```

## 4. Tools Copilot asks to confirm

Copilot asks before every tool whose `readOnlyHint` is `false`. On this view those are the tools that write:

- `remember`, `remember_batch`, `update_memory`, `forget`, `create_edge`, `create_context`, `update_context`, `delete_context`, `feedback`

These run without a prompt and are what the Researcher agent can use: `recall`, `reference`, `recall_upcoming`, `load_pinned`, `get_context_info`, `list_contexts`, `list_tags`, `list`, `changes_since`, `bootstrap`.

Every listed tool has a `title` and explicit `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` annotations; `backend/tests/mcp_server/test_microsoft_profile.py` pins this list.

## 5. Validation targets

Microsoft's validation guidelines for agents ask for the following; whether they also apply to connectors is not stated. The repository checks what the code decides; the operator measures the deployment.

| Target | Where it is decided | How to check it |
|---|---|---|
| No redirects | Code | `/mcp`, `/mcp/`, `/mcp/w/<id>` and `/mcp/w/<id>/`, with and without `?profile=microsoft`, answer the request themselves: `200` with a token, `401` with `WWW-Authenticate: Bearer … resource_metadata="…"` without one, never `3xx` (`test_microsoft_profile.py`). The `resource_metadata` URL (`/.well-known/oauth-protected-resource`) answers `200`; only the path-suffixed form `/.well-known/oauth-protected-resource/mcp` answers `301` to it. On the deployment: `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -X POST https://example.com/mcp?profile=microsoft` prints `401` and no redirect URL. |
| HTTPS with TLS 1.2 or later, on the publisher's verified domain | The reverse proxy / load balancer | `openssl s_client -connect example.com:443 -tls1_1 </dev/null` must fail; `-tls1_2` and `-tls1_3` must connect. The certificate's name must be the verified domain. |
| Tool calls under 30 s | Mostly the deployment (database, vector store, embedding provider) | The server's own limits per tool are in `backend/src/mcp_server/tools/_constants.py` (`TOOL_TIMEOUTS`): `remember` 30 s, `recall` 60 s, `remember_batch` 300 s, most others 10–15 s, overridable per tool with `MCP_TIMEOUT_<TOOL>`. A limit above 30 s only matters when a call is that slow; measure the real distribution below. |
| Latency p50 ≤ 2 s, p75 ≤ 5 s, p99 ≤ 9 s | The deployment | Time `tools/call` for `recall` and `remember` from outside the network, through the same URL, with a test account's OAuth token: a small MCP client script that initializes once and then times a few hundred calls of each, spread over a day, and reports the percentiles. Count the whole round trip (what Copilot sees), not the server's own log timing. Use a test context, since `remember` stores memories. |
| 99.9 % availability | The deployment | An external uptime check against the MCP URL (expecting `401` without a token) and the provider's incident history. |

## 6. Where it lives in the code

- `backend/src/mcp_server/tools/_profiles.py` — `PROFILES["microsoft"]`, `GOOGLE_VIEW_PROFILES`, `is_google_view()` (true for both names).
- `backend/src/mcp_server/tools/_google_profile.py` — the shared prose overrides, `recall` annotations, instructions and reply scrub.
- `backend/tests/mcp_server/test_microsoft_profile.py` — the alias equals the google view on every path (list, both transports, instructions, replies, `recall`'s learning switch), the annotations Copilot checks, and the no-redirect paths.
- `backend/tests/api/test_oauth_microsoft_redirect_uri.py` — the exact Teams redirect URI.
- `backend/tests/integration/test_oauth_cross_workspace_client_db.py` — on PostgreSQL: a client created by one account is authorized by a user of another workspace, whose calls run as that user and whose refresh token rotates, with either client authentication method.

A profile is a view, not an authorization boundary ([Tool Profiles](mcp-tools.md#tool-profiles)): `tools/call` still accepts any registered tool under the usual role checks.
