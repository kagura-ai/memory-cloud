# Remote OAuth Verification Runbook

When to use: you need dated, redacted evidence that a deployment's Remote MCP
authorization works end to end: discovery, Dynamic Client Registration
(DCR), PKCE, consent, token exchange, authenticated MCP over Streamable HTTP,
refresh and revocation. Typical triggers:

- **Release**: every release that touches authentication, the OAuth routes or
  the MCP transport (`backend/src/api/routes/oauth.py`,
  `backend/src/auth/`, `backend/src/api/routes/well_known.py`,
  `backend/src/mcp_server/transport*.py`).
- **Directory review**: before a submission to the Anthropic Software
  Directory or a re-review, against the release under review.
- **Investigation**: a client reports that connecting or reconnecting fails
  and you want to know which step breaks.

The script is `backend/scripts/verify_remote_oauth.py`. It needs Python 3.11
and `httpx` (the backend virtualenv has both) and never imports the server
code. Everything is automatic except one browser consent.

## What each step proves

Steps marked **required** decide the exit status: `0` only when every
required step passed. The others add evidence and never fail the run.

| Step | Required | Proves |
|---|---|---|
| D1 | yes | An unauthenticated `POST /mcp` answers `401` with a `Bearer` challenge carrying `resource_metadata`, without a redirect. |
| D2 | yes | Protected-resource metadata (RFC 9728): `resource` is the MCP endpoint, authorization servers are listed, header bearer tokens are accepted. Also records what the path-suffixed metadata URL answers. |
| D3 | yes | Authorization-server metadata (RFC 8414): issuer, authorization/token/registration/revocation endpoints (all `https` when the target is), `S256`, the `authorization_code` and `refresh_token` grants, public clients (`none`). |
| D4 | no | The OpenID discovery document names the same endpoints and PKCE methods. |
| D5 | yes | The deployed version from the public system-info endpoint (`--expect-version` makes a mismatch fail). |
| R1 | yes | DCR of a fresh public client with a loopback redirect: `201`, a `client_id`, no client secret, auth method `none`, the requested grant and response types, a scope. |
| R2 | yes | DCR accepts Claude's redirect URI `https://claude.ai/api/mcp/auth_callback` with the same properties. That client is never used further. |
| A0 | yes | The authorization endpoint issues no code without a signed-in session. |
| A1a-c | no | Authorization without `code_challenge`, with `plain`, and with an unregistered `redirect_uri`, before sign-in. A server that asks for sign-in first cannot be observed here; the step is then skipped and X1-X3 cover the case. |
| A2 | yes | The consent: S256 `code_challenge`, `state`, `resource` (RFC 8707) and scope; the redirect's `state` is verified. |
| T1-T3 | yes | The token endpoint rejects a request without `code_verifier`, with a wrong one, and with a different `redirect_uri`. These run on the consent's code, before the real exchange. |
| T4 | yes | The exchange with the right verifier: `Bearer`, `expires_in`, scope, whether a refresh token was issued. Passing it after T1-T3 also shows that a rejected request does not consume the code. |
| T5 | yes | Replaying the code is rejected (single use). |
| S1 | no | Introspection (RFC 7662): the token is active, its audience is the requested resource, it belongs to the registered client. |
| S2 | yes | A refresh cannot widen the granted scope. |
| S3 | no | The REST API enforces scope: a token narrowed to `memory:read` by a refresh is refused on a `POST` with `insufficient_scope`. The probe (`POST /api/v1/memory/recall` without a context) reads and writes nothing even if it were let through. |
| S4 | no | MCP applies the same token's scope: `list_contexts` succeeds, and a `remember` call answers `403` with `WWW-Authenticate: Bearer error="insufficient_scope"` naming `memory:write`. The call targets a context id that cannot exist, so it never stores anything; if a memory were stored anyway, the step fails and deletes it. |
| M1 | yes | MCP `initialize` with the OAuth token: protocol version, server version, `Mcp-Session-Id` presence, and the `instructions` length and whether it equals the static text in the checkout. |
| M2-M5 | yes | `notifications/initialized`, `tools/list` (every tool has a `title` and the four annotation hints; counts of read-only, destructive and open-world tools), one read call (`list_contexts`), and reuse of the session. |
| M6 | no | The stateless per-request era (MCP 2026-07-28): `server/discover` and `tools/list` without a session. |
| M7 | no | A session-era request (`tools/list` to `POST /mcp`) carrying an `Mcp-Session-Id` the server never issued, as a client sends after its session expired, is answered `404` with re-initialize guidance. |
| F1-F4 | yes (F3 no) | The refresh grant issues a new pair; the previous refresh token no longer works (rotation); what the previous access token gets; `initialize` works with the refreshed token. |
| V1-V2 | yes | After RFC 7009 revocation, `/mcp` answers `401` with the discovery challenge (the signal a client acts on to reconnect), and the revoked refresh token is refused. |
| V3 | no | Revoking an unknown token answers `200`. |
| V4 | yes | A bogus bearer token gets `401` with the discovery challenge. |
| V5 | no | The challenges recorded by V1 (revoked token) and V4 (bogus token) carry `error="invalid_token"` (RFC 6750 §3.1). |
| X1-X4 | no | Only with `--extended-authorize-checks`, signed in: an unregistered `redirect_uri` is not redirected to (operator-observed); a code issued without `code_challenge`, or with `plain`, yields no token; what happens to an undefined scope. |
| C1 | no | Every token the run obtained is revoked before exit. |
| C2 | no | DCR registrations are deleted through RFC 7592 when the server offers it; otherwise the step records that they remain (see [Cleanup](#cleanup-after-a-run)). |

## Running it

Run from a checkout of the deployed release, so the version and
`instructions` comparisons refer to the same code.

```bash
cd backend
.venv/bin/python scripts/verify_remote_oauth.py \
  --base-url https://memory.example.com \
  --label production \
  --expect-version 0.79.0 \
  --out ~/verification/remote-oauth-production.json \
  --markdown-out ~/verification/remote-oauth-production.md
```

Keep the output files outside the repository. `--help` lists every option;
the useful ones:

| Option | Use |
|---|---|
| `--discovery-only` | Steps D1-D5 only: no registration, no consent. A quick check after a deploy. |
| `--extended-authorize-checks` | Adds X1-X4: three more consents and one look at an error page. |
| `--scope` | Scope to request (default `memory:read memory:write`). |
| `--callback-port` | A fixed loopback port instead of any free one. |
| `--consent-timeout` | Seconds to wait for each redirect (default 600). |
| `--record-host` | Keep the real host in the evidence (see redaction below). |

### The consent step

After registration the script prints an authorization URL and waits:

1. Open the URL in a browser and sign in with the verification account
   (see [The verification account](#the-verification-account)), not your own.
2. Approve access.
3. The browser is redirected to `http://127.0.0.1:<port>/callback`, where the
   script listens. If that page cannot be reached (WSL2 with NAT networking,
   see [troubleshooting](../troubleshooting.md); or a browser on another
   machine), copy the full URL from the address bar and paste it into the
   terminal.

Type `abort` to stop; the script still revokes what it obtained and writes
partial evidence. With `--extended-authorize-checks`, `skip` skips one
extended check.

## Evidence and redaction

`--out` writes JSON with UTC timestamps, the script version, a run id, the
deployed version and environment, the checkout version, and per step: status
(`pass`, `fail`, `skip`, `info`), a one-line summary, the sub-checks and the
recorded values. The Markdown summary is printed to stdout (and written with
`--markdown-out`) for pasting into the tracking issue.

What never appears in either file:

- Access and refresh tokens, authorization codes, code verifiers and
  challenges, `state`, client secrets, cookies and MCP session ids: presence
  and length only.
- Client ids: a short SHA-256 fingerprint. The registrations are identified
  by their `client_name`, which ends with the run id.
- The target host: replaced by `<label>`; any other non-loopback host by
  `<label-other-N>`. `--record-host` keeps it.
- Tool results: `list_contexts` is recorded as a size and a count.

Before either file is written it is checked for every secret the run handled;
a match aborts the write. The authorization URL printed for the consent is
operator output, not evidence: do not paste the terminal log into an issue.
Read the Markdown once before posting it.

### Local and production evidence

A reader tells them apart from the evidence itself:

| Field | Production | Local |
|---|---|---|
| `target.label` | `production` (by convention) | `local` |
| `target.scheme` | `https` | `http` (only accepted for loopback) |
| `target.is_loopback` | `false` | `true` |
| `deployed.environment` | `production` | `development` |
| `deployed.version` | the release under review | the checkout |

Local runs exercise the same code paths but prove nothing about a deployment's
configuration (TLS, proxy routing, runtime settings). Only production evidence
closes a production question.

For a local run, start Postgres, Redis and Qdrant (`docker compose up -d
postgres redis qdrant`), apply migrations (`alembic upgrade head` from
`backend/`) and run the API on `127.0.0.1:8080` with `FRONTEND_URL` set to
that origin, so the discovery documents advertise endpoints the script can
reach. Create a local password user with `python -m src.cli.seed_e2e_admin`
(`E2E_ADMIN_LOGIN_ID` / `E2E_ADMIN_PASSWORD`); it is an administrator, which is
acceptable only on a throwaway local stack. Without the web UI on the same
origin there is no sign-in page: put the API and the web UI behind one local
origin (the reverse-proxy layout in [deployment](../deployment.md)), or sign in
through the API with a local-only helper and paste the redirect into the
script. Such a helper is deliberately not part of the repository: the script
never automates consent.

## The verification account

One dedicated account serves both these runs and Directory review. Never run
the script with a personal or administrator account.

- **Account**: a regular user, not a system or workspace administrator, with
  password sign-in. If a second factor is enabled, it must be shareable through
  the same channel as the password, because reviewers sign in too.
- **Workspace**: its own workspace with no other members.
- **Sample data**: one context (for example `directory-sample`) holding 5-10
  short memories written for the purpose: fictional project notes, a how-to,
  a decision record, a few tags. No personal data, no real customer or company
  data, no credentials. Enough that `list_contexts`, `recall` and `explore`
  return something meaningful.
- **Credentials**: kept in the team's secret store and shared with reviewers
  only through the submission form. Never in the repository, issues, pull
  requests, evidence files or chat logs.

### Cleanup after a run

- **Tokens**: revoked by the script (C1). Check that C1 passed.
- **DCR registrations**: two per run. When the server offers no RFC 7592
  deletion (C2 is `info`), an operator removes them from the deployment's
  database by the run id in their `client_name`; their tokens are deleted with
  them (`ON DELETE CASCADE`):

  ```sql
  DELETE FROM oauth_clients
  WHERE owner_id IS NULL
    AND client_name LIKE 'Claude remote OAuth verification <run id>';
  ```

- **Authorization codes** that were never exchanged (extended checks) become
  unusable after 10 minutes. **MCP sessions** expire after an hour of inactivity.
- **Sample data**: leave it; it is the reviewers' starting point. Remove only
  what a run added by mistake.

### After a Directory review

When the review is finished, rotate the account's password, revoke the tokens
issued to the account and delete the DCR registrations the reviewers' clients
created. Keep the account and its sample context for the next review.

## Re-running

Re-run on every release that touches authentication or the MCP transport, and
before each submission or re-review. Compare the new Markdown with the last
one in the tracking issue: a step that changes status is the thing to explain.
