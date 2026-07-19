# MCP Client Setup

How to connect MCP clients (Claude Code, Claude Desktop/Chat, ChatGPT, Gemini CLI, and any Streamable-HTTP client) to a self-hosted Kagura Memory Cloud. Start from the [README Quick Start](../README.md#quick-start) if your server isn't running yet; the full tool list is in the [MCP Tools Reference](mcp-tools.md).


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

Run `/kagura-memory:guide` for setup help and an optional SessionStart hook you can add to your project.

> **Prerequisite:** MCP connection must be configured (`.mcp.json` with API key). Run `/kagura-memory:guide` in your project to set it up.

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
2. Enter the MCP endpoint URL: `https://your-domain.com/mcp/w/{workspace_id}`
3. Add the `Authorization: Bearer kagura_{your_api_key}` header

> Claude Chat requires a publicly accessible URL (not `localhost`). Use a production deployment or tunnel (e.g., ngrok, Cloudflare Tunnel).

## ChatGPT Desktop

ChatGPT desktop app supports MCP servers. Add via Settings > MCP Servers:
1. Server URL: `https://your-domain.com/mcp/w/{workspace_id}`
2. Authentication: Bearer token `kagura_{your_api_key}`

> Like Claude Chat, ChatGPT requires a public URL. For local development, use a tunnel or the REST API directly.

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

## Other MCP Clients / REST API

Any MCP-compatible client can connect via Streamable HTTP. For clients without MCP support, use the REST API directly:

```bash
# Search memories
curl -X POST -H "Authorization: Bearer kagura_{your_key}" \
  -H "Content-Type: application/json" \
  -d '{"query": "your search", "context_id": "..."}' \
  http://localhost:8080/api/v1/memories/search
```
