# MCP Client Setup

How to connect MCP clients (Claude Code, Claude Desktop/Chat, ChatGPT, Gemini CLI, and any Streamable-HTTP client) to a self-hosted Kagura Memory Cloud. Start from the [README Quick Start](../README.md#quick-start) if your server isn't running yet; the full tool list is in the [MCP Tools Reference](mcp-tools.md).

## Which URL?

Every client below takes one of two endpoint URLs — same server, same API key:

| | Endpoint URL |
|---|---|
| **All tools (default)** | `…/mcp/w/{workspace_id}` |
| **Core tools only — smaller tool list** | `…/mcp/w/{workspace_id}?profile=core` |
| **Guardrail digest** (clients without tool hooks) | `…/mcp/w/{workspace_id}?guardrails=<context_id>` (off: `?guardrails=off`) |

Pick core when your client loads every tool schema at session start (it is about 65% smaller). It lists the 12 memory and context tools and leaves out Sleep, analyses, files, edges, secrets, resources and the agent control plane — those stay callable, they are just not listed; switch back to the default URL to see them. The exact tool set and sizes are in [Tool Profiles](mcp-tools.md#tool-profiles); a narrower allowlist (`?tools=…`) is under [List fewer tools](#list-fewer-tools). The Web UI's MCP Setup Guide has a **Core tools only** switch that writes the query into its snippets for you.

## Claude Code (Recommended)

Claude Code + Kagura Memory Cloud gives your AI assistant **persistent, searchable, team-shareable memory** that works across sessions, machines, and projects.

**Why not just use Claude Code's built-in memory?**

| | Claude Code Memory | Kagura Memory Cloud |
|---|---|---|
| Storage | Local files (`~/.claude/`) | Cloud (PostgreSQL + Qdrant) |
| Search | File name only | Hybrid Search (semantic + full-text) |
| Sharing | Single machine | Team workspace with RBAC |
| Structure | Flat markdown | 3-layer architecture + Neural Memory graph |
| Cross-project | Per-project only | Any project via MCP |

**Setup (3 steps):**

1. Start services and open `http://localhost:3000/workspace/integrations/api-keys` to create an API key
2. Copy `.mcp.json.example` to `.mcp.json` and fill in your workspace ID and API key:

```bash
cp .mcp.json.example .mcp.json
# Edit .mcp.json — set workspace_id (from URL bar) and API key
```

`.mcp.json.example` ships with the all-tools URL (JSON has no comments, so the choice is spelled out here). Set `"url"` to one of — see [Which URL?](#which-url):

- **All tools (default):** `http://localhost:8080/mcp/w/{workspace_id}`
- **Core tools only — smaller tool list:** `http://localhost:8080/mcp/w/{workspace_id}?profile=core`

3. Restart Claude Code and verify:
```
You: "List my memory contexts"
→ AI calls list_contexts()

You: "Remember: our API uses JWT with 1h expiry and refresh token rotation"
→ AI calls remember() — stored permanently

You: "What do we know about auth?"
→ AI calls recall() — finds it instantly, even months later
```

> `.mcp.json` is in `.gitignore` — never commit it (contains API keys).
> Place in project root for per-project config, or `~/.claude/.mcp.json` for global access.

<details>
<summary>Auto-sync Claude Code memory to Kagura (optional)</summary>

Kagura Memory Cloud includes a hook that **automatically syncs** Claude Code's local memory files to the cloud whenever they're updated. This means your local `~/.claude/` memories are also searchable via Hybrid Search and shared with your team.

Add these environment variables to `.env.local`:

```bash
KAGURA_MCP_URL=http://localhost:8080/mcp/w/{workspace_id}
KAGURA_MCP_TOKEN=kagura_{your_api_key}
KAGURA_CONTEXT_ID={context_id}  # optional — auto-detected from project name if omitted
```

The sync hook is pre-configured in `.claude/settings.json` and runs automatically on every memory file write.

</details>

<details>
<summary>Claude Code integration templates (copy to your project)</summary>

This repo's `.claude/` directory is a **ready-to-use template** for integrating Kagura Memory Cloud into any project. Copy what you need:

**Slash commands** — drop into your project's `.claude/commands/`:
```
/recall <query>   → Search past knowledge via MCP
/remember <text>  → Save decisions, patterns, learnings via MCP
/guide            → Show setup and usage guide
```

**Hooks** (`.claude/settings.json`) — auto-configured safety guards:
```
Auto-format    → ruff (Python) / prettier (TypeScript) on every save
Secret detect  → Blocks hardcoded API keys or passwords
SQL injection  → Blocks f-string SQL (enforces parameterized queries)
Memory sync    → Auto-syncs Claude Code memory files to Kagura Cloud
```

**Agents** (`.claude/agents/`) — specialized sub-agents:
```
code-reviewer  → Read-only code review against project standards
test-runner    → Run tests, diagnose failures, auto-fix
```

**Rules** (`.claude/rules/`) — auto-loaded project conventions:
```
backend.md     → FastAPI/Python patterns, async, testing
frontend.md    → Next.js/TypeScript, Tailwind, SWR
security.md    → Auth, RBAC, SQL safety, CORS
```

All of these are designed to work together with Kagura Memory Cloud MCP tools. Adapt them for your own project by editing the prompts and context IDs.

</details>

<details>
<summary>Claude Code Plugin (use Kagura in any project)</summary>

The **kagura-memory** plugin adds session management and memory workflow skills to Claude Code. Install it once and use it across all your projects.

**Install:**

```bash
# From marketplace
/plugin marketplace add kagura-ai/memory-cloud
/plugin install kagura-memory@kagura-memory-cloud
```

**Available skills:**

| Skill | Description |
|-------|-------------|
| `/kagura-memory:session-start` | Restore previous session context on start |
| `/kagura-memory:session-summary` | Save session knowledge before ending |
| `/kagura-memory:recall` | Search past knowledge |
| `/kagura-memory:remember` | Save new knowledge |
| `/kagura-memory:guide` | Usage guide, connection status, and setup help |
| `/kagura-memory:setup` | Configure and verify the MCP connection and the guardrail hooks (`--check` = read-only doctor) |
| `/kagura-memory:smoke-test` | Verify all MCP tools work |

**Recommended workflow:**

```
/kagura-memory:session-start       # ← Start here: restore context from last session
  ... work normally ...
/kagura-memory:recall              # Search past decisions, patterns, fixes
/kagura-memory:remember            # Save important learnings as you go
  ... finish work ...
/kagura-memory:session-summary     # ← End here: save session knowledge for next time
```

Skills wrap the raw MCP tools (`recall`, `remember`, etc.) with workflow logic — context selection, git state analysis, and structured prompts. Use skills for session management and guided workflows; use MCP tools directly for fine-grained operations.

Run `/kagura-memory:guide` for setup help; its section 5 covers the plugin's tool-guardrail hooks below.

> **Prerequisite:** MCP connection must be configured (`.mcp.json` with API key). Run `/kagura-memory:guide` in your project to set it up.

**Tool guardrails (hooks)**

The plugin also declares Claude Code hooks (`claude-hooks/hooks.json`, one Python 3.9+ stdlib script under `plugins/kagura-memory/hooks/`) that deliver [tool guardrails](mcp-tools.md#tool-guardrails) — memories marked with `details.tool_trigger` — at the matching tool call, in the main thread and in subagents. They do nothing until configured.

- **Setup:** Claude Code prompts for `server_url` (the MCP endpoint from `.mcp.json`), `api_key` (a user API key, stored as a sensitive value), `context_id` (the UUID of the context whose guardrails apply) and `max_action` (`block`, the default, or `inform`) when the plugin is enabled. The values live in user settings and the keychain only — a project's `.claude/settings.json`, `.mcp.json`, `~/.claude.json` and any `KAGURA_*` variable are never read. Scriptable: `claude plugin install kagura-memory@kagura-memory-cloud --config server_url=https://<your-domain>/mcp/w/<workspace-id> --config api_key=<your API key> --config context_id=<context uuid>`.
- **Guided setup:** `/kagura-memory:setup` does all of the above and verifies it — it names the MCP entry actually in effect (a `local`-scope entry silently shadows a `project` one of the same name), derives `server_url` from that entry's URL, warns before touching an OAuth URL, and runs the hook once so the result is "fetched N guardrails" rather than silence. `/kagura-memory:setup --check` is the read-only doctor.
- **One guardrail lane per client.** With the plugin hooks on, put `?guardrails=off` on the MCP URL in `.mcp.json` (`https://<your-domain>/mcp/w/<workspace-id>?guardrails=off`, or `&guardrails=off` when the URL already has a query, such as `?profile=core`); the hooks are the guardrail lane for this client, and the server then sends no digest of the same memories. The plugin's `server_url` stays the plain endpoint.
- **Migrating an existing entry:** changing the MCP URL of an entry authenticated with **OAuth** requires re-authentication. Claude Code stores OAuth tokens per endpoint, so adding `?guardrails=off` leaves the entry with no token — the server disconnects and every Kagura tool disappears until you re-run `/mcp` and sign in again. Plan the edit and the sign-in together, or leave the URL alone; the duplicate digest costs tokens, nothing more. A Bearer-key entry is unaffected. A `kagura-mcp` entry (from `kagura setup claude --profile <name>`) has no URL in `.mcp.json` and the CLI profile cannot carry the query; the proxy's own flags put it on the upstream URL instead. With `kagura-mcp` 0.39.0 or later (`kagura --version`), add `"--guardrails", "off"` to its `args` (or replace an existing `--guardrails <context-id>` value with `off`), or re-run `kagura setup claude --profile <name> --guardrails off`; `--tool-profile <name>` sets `?profile=` the same way, and each flag replaces that parameter in any `--server` query. Before 0.39.0 the only way is `"--server", "https://<your-domain>/mcp?guardrails=off"` in its `args` (same host as the profile — the proxy sends the profile's token there), which a later `kagura setup claude` re-run drops. Either way no re-authentication is needed.
- **`server_url` is the MCP endpoint, not the site root.** The hook POSTs `tools/call load_guardrails` to `server_url` exactly as given, so it must end in `/mcp` or `/mcp/w/<workspace-id>`. A site root answers `http 405`, and the session-start notice then says so instead of reporting the server unreachable.
- **What is sent where:** `SessionStart` makes one `tools/call load_guardrails` to `server_url` with `{"context_id"}` as the only argument (https, or http on localhost; redirects are refused) and caches the result at `~/.claude/plugins/data/kagura-memory-*/guardrails/<context_id>.json` (mode 0600). `PreToolUse`, `PostToolUse` and `PostToolUseFailure` read that cache only — tool inputs never leave the machine. A `remember` / `update_memory` / `forget` call refreshes the cache in the background.
- **What Claude sees:** an `inform` guardrail as context next to the tool result (`Kagura Memory guardrail (<id8>): <summary>`, `, by another member` when someone else wrote it); a `block` guardrail as a one-time deny with the memory as the reason — the re-issued call proceeds. Once per guardrail per session and agent, at most 3 lines per call and 10 `inform` lines per session-agent; `/clear` and `/compact` reset the main thread's markers. `block` is a speed bump, not enforcement: permission deny rules remain the enforcement tool.
- **What you see:** one notice at session start naming new, changed or removed guardrails, or a missing configuration field; nothing when nothing changed.
- **Off switch:** `max_action: inform` stops denies; disabling the plugin or `claude --settings '{"disableAllHooks": true}'` stops the hooks. Fail-open everywhere: a missing `python3`, an unreachable server (a cache up to 24 h old is used at session start; tool events accept 7 days), a corrupt cache or an unsupported pattern never blocks a call. Windows is unsupported for these hooks (install Git Bash or disable them).

</details>

## WSL2 + Claude Code (NAT networking note)

If you run Claude Code **inside WSL2** and use the **OAuth** flow (not the API-key setup above), the browser callback can fail silently: WSL2's default `nat` mode isolates `localhost` between Windows and WSL, so the Windows browser's redirect to `localhost:<port>` never reaches Claude Code's listener inside WSL. **This is a WSL networking issue, not a Kagura server bug** — the resolved server-side cousin was [#689 / PR #692](https://github.com/kagura-ai/memory-cloud/pull/692).

Quick fixes:

- **Enable mirrored networking** — set `networkingMode=mirrored` in `C:\Users\<YourName>\.wslconfig`, then `wsl --shutdown` (requires WSL 2.0.0+ on Windows 11 22H2+).
- **Use API key (Bearer) auth** — the `.mcp.json` setup above skips the OAuth callback entirely.
- **Use the device flow** ([#635 / PR #636](https://github.com/kagura-ai/memory-cloud/pull/636), RFC 8628) if your client supports it.

See [Troubleshooting → WSL2 + Claude Code](troubleshooting.md#wsl2--claude-code--mcp-oauth-callback-fails-default-nat-networking) for the full symptom → diagnosis → fix walkthrough.

## Claude Desktop / Claude Chat (Web)

**Claude Desktop**: Same `.mcp.json` format as Claude Code — place in your project root or `~/.claude/.mcp.json`.

**Claude Chat (claude.ai)**: Add as a remote MCP server in Settings > Integrations:
1. Click "Add Integration" → "Custom MCP Server"
2. Enter the MCP endpoint URL ([which one?](#which-url)):
   - **All tools (default):** `https://your-domain.com/mcp/w/{workspace_id}`
   - **Core tools only — smaller tool list:** `https://your-domain.com/mcp/w/{workspace_id}?profile=core`
3. Add the `Authorization: Bearer kagura_{your_api_key}` header

> Claude Chat requires a publicly accessible URL (not `localhost`). Use a production deployment or tunnel (e.g., ngrok, Cloudflare Tunnel).

Neither client runs tool hooks, so the server sends its `instructions` on `initialize` — the base text plus, with `?guardrails=<context_id>` on the URL (or an agent-bound key with a default binding), a digest of that context's tool guardrails; whether the client surfaces them to the model is the client's choice, and the `get_context_info.guardrails` block at session start does not depend on it ([MCP Tools › Server instructions](mcp-tools.md#server-instructions)).

## ChatGPT Desktop

ChatGPT desktop app supports MCP servers. Add via Settings > MCP Servers:
1. Server URL ([which one?](#which-url)):
   - **All tools (default):** `https://your-domain.com/mcp/w/{workspace_id}`
   - **Core tools only — smaller tool list:** `https://your-domain.com/mcp/w/{workspace_id}?profile=core`
2. Authentication: Bearer token `kagura_{your_api_key}`

> Like Claude Chat, ChatGPT requires a public URL. For local development, use a tunnel or the REST API directly.

### ChatGPT web (developer mode)

ChatGPT web (and ChatGPT Work on the web) runs no client-side hooks, so [tool guardrails](mcp-tools.md#tool-guardrails) reach the model through the server instead:

- **Server instructions** — add `?guardrails=<context_id>` to the connector's Server URL, in the same query as `?profile=` (`https://your-domain.com/mcp/w/{workspace_id}?profile=core&guardrails=<context_id>`). `server/discover` then returns the base instructions plus a digest of that context's tool guardrails: up to 5 entries, one line each, at most 1,200 characters in total. ChatGPT reads the instructions at connect time and on **Refresh** in developer mode — the digest is a snapshot until the next refresh. `?guardrails=off` switches both server lanes off.
- **`get_context_info.guardrails`** — on by default for every URL: the session-start call returns the context's guardrails per call, nothing to configure.

Who writes what you see: a context editor or above, with a user credential (an agent-bound key cannot author a guardrail). The digest reaches **every** conversation of the connector, and a connector configured with one shared API key serves that key's digest to every user of the connector. Use `?guardrails=<context_id>` only for a context whose editor list you control; for a shared workspace context prefer the per-session `get_context_info.guardrails` lane. Preview exactly what a credential receives with `GET /api/v1/memory/guardrails/digest?context_id=<uuid>&target=instructions` ([API Reference](api-reference.md#get-apiv1memoryguardrailsdigest)); the full rules are in [MCP Tools › Server instructions](mcp-tools.md#server-instructions).

## Gemini CLI

Add to `.gemini/settings.json` (project root or `~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "kagura-memory": {
      "url": "http://localhost:8080/mcp/w/{workspace_id}",
      "headers": {
        "Authorization": "Bearer kagura_{your_api_key}"
      }
    }
  }
}
```

That `"url"` is the **all tools (default)** one. For **core tools only — smaller tool list**, use `"http://localhost:8080/mcp/w/{workspace_id}?profile=core"` ([which one?](#which-url)).

## Codex cloud

Codex cloud tasks run no MCP client hooks and read the repository's `AGENTS.md` before doing any work, so the lane for [tool guardrails](mcp-tools.md#tool-guardrails) there is an export block written into `AGENTS.md` by the environment's **setup script**. The server renders the block (`GET /api/v1/memory/guardrails/digest`, `text/markdown` — [API Reference](api-reference.md#get-apiv1memoryguardrailsdigest)); the recipe below writes it between two marker lines and keeps it out of the task's diff.

**Environment.** `KAGURA_API_KEY` as a Codex cloud **secret** — secrets are available to setup scripts only and removed before the agent phase; `KAGURA_API_BASE` and `KAGURA_CONTEXT_ID` as plain environment variables. `KAGURA_API_BASE` is the scheme and host (and port) of your deployment, `https://<your-domain>` — **not** the MCP URL: the recipe refuses a value that contains `/mcp`, carries a path, or does not start with `https://`. `KAGURA_API_KEY` and `KAGURA_CONTEXT_ID` are checked before use, so an unset variable is a reported skip, not a `set -u` abort. Use the narrowest key you have: an agent-bound key whose only binding is that context, read access only; never your personal key, and never a key as an environment variable (those reach the agent phase). The default cloud image has `bash`, `curl`, `python3` and `git`; each is checked, and a missing one is a single `stderr` line.

```bash
# Kagura Memory guardrails → AGENTS.md (setup script; never fails the setup, says why on stderr)
set -u
say() { echo "kagura guardrails: $*" >&2; }
BASE="${KAGURA_API_BASE:-}"; BASE="${BASE%/}"
case "$BASE" in
  https://*"/mcp"*) say "KAGURA_API_BASE must not contain /mcp (it is not the MCP URL); AGENTS.md unchanged"; exit 0 ;;
  https://*/*) say "KAGURA_API_BASE must be https://<host> (scheme and host only, no path); AGENTS.md unchanged"; exit 0 ;;
  https://?*) ;;
  *) say "KAGURA_API_BASE must be https://<host> (scheme and host only); AGENTS.md unchanged"; exit 0 ;;
esac
[ -n "${KAGURA_API_KEY:-}" ] || { say "KAGURA_API_KEY is not set (add it as a secret); AGENTS.md unchanged"; exit 0; }
case "${KAGURA_CONTEXT_ID:-}" in
  "" | *[!0-9a-fA-F-]*) say "KAGURA_CONTEXT_ID must be the context UUID; AGENTS.md unchanged"; exit 0 ;;
esac
for t in curl python3 git; do command -v "$t" >/dev/null 2>&1 || { say "$t not found; AGENTS.md unchanged"; exit 0; }; done
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
BLOCK="$(curl --fail --silent --max-time 10 \
  -H "Authorization: Bearer ${KAGURA_API_KEY}" \
  "${BASE}/api/v1/memory/guardrails/digest?context_id=${KAGURA_CONTEXT_ID}")" \
  || { say "fetch failed (curl exit $?); AGENTS.md unchanged"; exit 0; }
[ -n "$BLOCK" ] || say "no guardrails in context; an earlier block is removed, none is written"
MSG="$(KAGURA_BLOCK="$BLOCK" KAGURA_AGENTS_MD="$ROOT/AGENTS.md" python3 - <<'PY' 2>&1
import os, re, sys, tempfile
path = os.environ["KAGURA_AGENTS_MD"]
block = os.environ["KAGURA_BLOCK"].strip()
begin_re = re.compile(r"^<!-- kagura-memory:guardrails begin[^\n]*$", re.M)
end_re = re.compile(r"^<!-- kagura-memory:guardrails end -->$", re.M)
if block and (len(begin_re.findall(block)) != 1 or len(end_re.findall(block)) != 1):
    sys.exit("fetched block does not have exactly one begin and one end marker line")
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
span = re.compile(begin_re.pattern + r".*?" + end_re.pattern + r"\n?", re.M | re.S)
if len(begin_re.findall(text)) > 1 or len(end_re.findall(text)) > 1:
    sys.exit("AGENTS.md has more than one guardrail block; fix it by hand")
if not block:  # empty digest: drop an earlier block (and the blank line before it), never create one
    new = re.sub(r"\n?" + span.pattern, "", text, count=1, flags=re.M | re.S)
elif span.search(text):
    new = span.sub(lambda _m: block + "\n", text, count=1)
else:
    new = text + ("" if not text or text.endswith("\n") else "\n") + "\n" + block + "\n"
if new != text:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".AGENTS.md.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)
PY
)" || { say "write failed: ${MSG##*$'\n'}; AGENTS.md unchanged"; exit 0; }
# Keep the block out of the task's diff: it is workspace memory, not repository content.
( cd "$ROOT" && git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  if git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then git update-index --skip-worktree AGENTS.md
  else git check-ignore -q AGENTS.md || echo AGENTS.md >>"$(git rev-parse --git-path info/exclude)"; fi ) 2>/dev/null || true
```

- `https` only and no `-L`: `curl` never follows a redirect, so the key cannot travel to another host.
- Every failure prints exactly one `kagura guardrails: …` line to the setup log and exits 0 — the setup never fails because of the block, and a misconfiguration stays visible. `curl` runs silent (its exit code is in the line) and a refusal from the Python half is folded into the same line, so nothing else reaches `stderr`.
- The block never leaves the container: `git update-index --skip-worktree AGENTS.md` keeps the modified tracked file out of `git status` and out of the task's diff; when `AGENTS.md` is not tracked (the recipe created it), it is listed in the repository's local `.git/info/exclude` instead, which is never committed. If you see the block in a task diff, that step did not run. A later legitimate edit of a tracked `AGENTS.md` in that container needs `git update-index --no-skip-worktree AGENTS.md` first.
- Re-running the script replaces the block in place. The markers are matched at line start; a fetched block with more or fewer than one begin or one end line is refused and the file is left unchanged. A guardrail removed on the server disappears from the block on the next run, and when the context has no tool guardrails left (an empty `200`) an earlier block is removed and no block is written — the file never keeps a guardrail the server no longer serves.
- The safer variant for a public repository: point `KAGURA_AGENTS_MD` at an untracked file and keep a one-line pointer to it in the tracked `AGENTS.md`. The same block works in any always-loaded file (`CLAUDE.md`, `GEMINI.md`, `.cursor/rules`) by changing `KAGURA_AGENTS_MD`.
- The cloud documentation implies the order checkout → setup script → agent but does not state that `AGENTS.md` is read after the setup script; confirm with one task before relying on it.

The recipe is extracted from this page and exercised by `backend/tests/api/test_guardrail_export_snippet.py`.

## List fewer tools

By default `tools/list` returns all 64 tool definitions (≈ 84k characters of JSON). A client that puts every schema into the model's context when a session starts pays for that in each session. To list only what you use, add a query parameter to the endpoint URL your client already stores:

| URL suffix | `tools/list` returns |
|---|---|
| `?profile=core` | The 12 core memory tools — ≈ 28k characters, about 65% smaller |
| `?tools=remember,recall,reference` | Exactly the named tools (an allowlist; wins over `profile`) |
| *(none)* or `?profile=full` | Everything — the default |

Only the URL changes; the `Authorization` header stays as it is. The client sections above show the core URL in full; this is where the URL lives in each client's configuration:

| Client | Where the URL goes |
|---|---|
| Claude Code | `"url"` in `.mcp.json` |
| Claude Desktop / Claude Chat | `"url"` in the same `.mcp.json` shape / the endpoint URL of the custom MCP server |
| ChatGPT | Server URL |
| Gemini CLI | `"url"` in `.gemini/settings.json` |
| Cursor | `"url"` of the `mcpServers` entry (same shape as Claude Code) |
| Codex CLI | `url = "http://localhost:8080/mcp/w/{workspace_id}?profile=core"` in `~/.codex/config.toml` |

Restart or reconnect the client afterwards so it lists tools again. A typo in `profile`, or a `tools` list that matches nothing, makes `tools/list` fail with an error naming the valid values rather than silently falling back to the full list.

`?guardrails=` lives in the same URL. `?guardrails=<context_id>` adds a digest of that context's tool guardrails to the server instructions; `?guardrails=off` switches both server-side guardrail lanes off — the instructions digest and the `get_context_info.guardrails` block — which is the setting for a client whose plugin hooks already deliver guardrails at the tool call. A value that is neither `off` nor a context id is ignored: the instructions stay at the base text and `get_context_info.guardrails` stays on. Rules and caps: [MCP Tools › Server instructions](mcp-tools.md#server-instructions).

> This changes what is **listed**, not what can be **called** — it is not an access control. See [MCP Tools Reference › Tool Profiles](mcp-tools.md#tool-profiles) for the exact rules and the core tool set.
>
> Clients that defer tool loading — fetching a tool's schema only when it is about to be used, as Claude Code's tool search does — gain little: they never paid for the whole list in the first place.

## Other MCP Clients / REST API

Any MCP-compatible client can connect via Streamable HTTP. For clients without MCP support, use the REST API directly:

```bash
# Search memories
curl -X POST -H "Authorization: Bearer kagura_{your_key}" \
  -H "Content-Type: application/json" \
  -d '{"query": "your search", "context_id": "..."}' \
  http://localhost:8080/api/v1/memories/search
```
