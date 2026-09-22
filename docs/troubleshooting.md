# Troubleshooting

Solutions to client-configuration and environment-specific setup problems. If your issue is not listed here, check the [Getting Started](getting-started.md) guide or open an issue.

## A tool I expect is missing from my client

**You are probably on `?profile=core`.** The client lists `remember`, `recall` and the other core tools, but not, say, `create_edge`, `list_files`, `get_sleep_report` or `secret_get`.

Look at the endpoint URL your client stores — `"url"` in `.mcp.json` / `.gemini/settings.json`, `url` in `~/.codex/config.toml`, or the URL field of the connector form:

| URL ends with | The client lists |
|---|---|
| `/mcp/w/{workspace_id}` | All tools (default) |
| `?profile=core` | Core tools only — the 12 memory and context tools. Sleep, analyses, files, edges, secrets, resources and the agent control plane are left out |
| `?tools=…` | Exactly the names in the list |

The missing tool is still **callable** — a profile filters the list, not access — but most clients only offer what they list. To see it, switch back to the default URL (or add its name to `?tools=`), then restart or reconnect the client so it lists tools again. Background: [Tool Profiles](mcp-tools.md#tool-profiles).

## Codex CLI — `bearer_token is not supported for streamable_http`

Codex fails to start, or `codex mcp list` fails, with:

```
bearer_token is not supported for streamable_http
```

**You copied a pre-fix snippet.** Earlier versions of this documentation and of the web UI's Codex tab wrote the API key into `~/.codex/config.toml` as `bearer_token = "…"`, next to a `type = "http"` line. Codex (checked against `rust-v0.155.1`) does not accept an inline token on an HTTP server, and because it validates `mcp_servers` as one map, the **whole `config.toml` fails to load** — every other server in the file is gone too — until the key is removed. `type` is not a Codex key either: it is ignored by default and an "unknown configuration field" error under `codex --strict-config`.

Replace the entry with the two keys Codex reads, and put the key itself in the environment Codex starts from:

```toml
[mcp_servers.kagura-memory]
url = "http://localhost:8080/mcp/w/{workspace_id}"
bearer_token_env_var = "KAGURA_API_KEY"
```

```bash
export KAGURA_API_KEY="kagura_{your_api_key}"
```

`codex mcp add kagura-memory --url "…" --bearer-token-env-var KAGURA_API_KEY` writes the same entry; see [Getting Started → Codex CLI](getting-started.md#codex-cli). Keep the key out of `config.toml`: `bearer_token_env_var` (or `env_http_headers`) is how Codex takes it from the environment.

## Codex CLI — kagura-memory hooks never run

The plugin is installed, but no guardrail ever reaches the model and `/hooks` shows no activity. Each item below is one visible symptom; the session-start notice (a UI warning, not model context) names the one that applies.

1. **Not trusted.** Codex skips plugin-bundled hooks until you review them: open `/hooks`, trust the `kagura-memory@kagura-memory-cloud` entries, restart. Codex prints a startup warning while a review is pending, and asks again after a release that changes `hooks/hooks.json` — the trust hash covers the hook definitions, not the script.
2. **Python.** The hooks need `python3` on `PATH` outside the project directory: 3.9+ to match, 3.11+ to read the credentials (`tomllib`). Below 3.11 the notice reads `Codex credentials need Python 3.11+; no fetch at session start or refresh, tool events use the existing cache only` — nothing is fetched, and a cache written earlier by a 3.11+ interpreter is still delivered.
3. **No `config.json`.** Without `${CODEX_HOME:-~/.codex}/plugins/data/kagura-memory-*/config.json` every hook is a silent no-op — ask the skill to "turn on Kagura guardrails". A non-UUID `context_id` prints `config.json context_id is missing or invalid; hooks stay idle`.
4. **Unsupported server entry.** The hooks read the user-level `[mcp_servers.kagura-memory]` only: `url` plus exactly one of `bearer_token_env_var`, `env_http_headers.Authorization`, `http_headers.Authorization`. An OAuth-only entry, an `http_headers_helper`, two Authorization sources, an unset variable or an `http://` URL that is not loopback each print one `mcp_servers.kagura-memory… is missing or invalid; hooks stay idle` notice, and the hooks make no request. A project `.codex/config.toml` is never read.
5. **Legacy marketplace.** If `/hooks` lists idle hooks whose commands mention `${CLAUDE_PLUGIN_ROOT}`, Codex loaded the repository root through `.claude-plugin/marketplace.json`; install from `.agents/plugins/marketplace.json` instead.
6. **`server unreachable` at session start.** A cache up to 24 hours old is still used; an older cache is renamed `.stale` and the hooks stay quiet until the server answers again.

## WSL2 + Claude Code — MCP OAuth callback fails (default NAT networking)

**This is a WSL networking issue, not a Kagura Memory Cloud server bug.** The server-side OAuth path is healthy — the resolved server-side cousin of this report was [#689 / PR #692](https://github.com/kagura-ai/memory-cloud/pull/692) (DCR no longer issues a `client_secret` for public `auth_method="none"` clients), deployed 2026-05-17. The remaining problem is purely the WSL2 NAT network isolation between the WSL listener and the Windows browser.

### Symptom

When you run Claude Code **inside WSL2** and start the MCP OAuth flow against a remote MCP endpoint (e.g. `https://your-domain.com/mcp`):

1. `wslview` (or `cmd.exe /c start`) launches the authorize URL in a **Windows** browser.
2. After consent, the browser redirects to `http://localhost:<port>/callback?code=...` — which hits **Windows'** loopback.
3. Claude Code's callback listener is bound on the **WSL side** `127.0.0.1:<port>` — so it never receives the redirect.
4. The browser shows "connection refused" / "site can't be reached". Manually pasting the callback URL into the WSL terminal recovers the flow, but the UX is broken.
5. Dynamic Client Registration (DCR) may run twice (two different `client_id` values and ephemeral ports), because the first attempt's listener never receives the callback and the SDK re-registers.

### Diagnosis

WSL2's **default `nat` networking mode** gives Windows and WSL2 each their **own** `127.0.0.1` loopback — packets do not cross between them. The OAuth auto-callback design (the RFC 8252 native-app loopback pattern) assumes a single `localhost` namespace, which `nat` mode violates.

You are affected if:

- `[wsl2] networkingMode` is unset or set to `nat` (the default), **and**
- Claude Code runs inside WSL with the browser launched via `wslview` / `cmd.exe /c start`.

Confirmed on WSL 2.6.1 + kernel `6.6.87.2-microsoft-standard-WSL2`.

### Fix 1 — Enable mirrored networking (recommended)

`mirrored` mode shares the Windows network stack with WSL, so `localhost:<port>` from the Windows browser reaches the WSL listener directly. Requires **WSL 2.0.0+ on Windows 11 22H2+**.

Edit `C:\Users\<YourName>\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Then, in PowerShell:

```powershell
wsl --shutdown
```

Restart WSL and retry the OAuth flow. See Microsoft's docs on [mirrored mode networking](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking) for prerequisites and known limitations.

### Fix 2 — Use the Device Authorization Grant (RFC 8628)

If your client supports the device-flow profile, it can avoid the localhost callback entirely. Kagura's `/device` endpoint is wired into the OAuth path ([#635 / PR #636](https://github.com/kagura-ai/memory-cloud/pull/636), [#639 / PR #642](https://github.com/kagura-ai/memory-cloud/pull/642), shipped 2026-05-13), and `kagura-cli` already uses this path. The device flow displays a user code to enter in any browser — there is no loopback redirect to be isolated by NAT.

### Fix 3 — Use API key (Bearer) auth instead of OAuth

OAuth is optional. You can skip the callback entirely by configuring a Bearer API key:

1. Open the web UI → **Workspace → Integrations → API Keys** and create a key (it looks like `kagura_...`).
2. Configure the `kagura-memory` MCP server in `~/.claude.json` (global) or your project's `.mcp.json`:

   ```json
   {
     "mcpServers": {
      "kagura-memory": {
        "type": "http",
        "url": "https://your-domain.com/mcp/w/{workspace_id}",
        "headers": {
          "Authorization": "Bearer kagura_{your_api_key}"
        }
      }
     }
   }
   ```

3. Restart Claude Code. No browser callback is involved, so NAT isolation no longer matters.

> `.mcp.json` is in `.gitignore` — never commit it (it contains API keys).

### Related

- [#689 / PR #692](https://github.com/kagura-ai/memory-cloud/pull/692) — DCR `client_secret` for `auth_method="none"` (the prior visible-error twin of this issue; independently fixed and deployed).
- [#635 / PR #636](https://github.com/kagura-ai/memory-cloud/pull/636) + [#639 / PR #642](https://github.com/kagura-ai/memory-cloud/pull/642) — Device Authorization Grant (RFC 8628) wired into the OAuth path.
