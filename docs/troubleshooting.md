# Troubleshooting

Solutions to environment-specific setup problems. If your issue is not listed here, check the [Getting Started](getting-started.md) guide or open an issue.

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
