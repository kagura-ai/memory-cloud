---
description: Show Kagura Memory Cloud usage guide, connection status, and setup help
---

Show the user how to use Kagura Memory Cloud. If not connected, help them set up the MCP connection.

## Steps

### 1. Check MCP connection

```
list_contexts()
```

If this succeeds, MCP is connected. Skip to Step 3.

If this fails, guide the user through setup (Step 2).

### 2. MCP connection setup (if not connected)

Check for existing config:

```bash
cat .mcp.json 2>/dev/null || echo "No .mcp.json found"
```

If no config exists, ask the user for:
- **Server URL**: Where their Kagura Memory Cloud instance is running (default: `http://localhost:8080`)
- **Workspace ID**: Found in the web UI URL bar after login
- **API key**: Created at Workspace > Integrations > API Keys (starts with `kagura_`)

Create or update `.mcp.json`:

```json
{
  "mcpServers": {
    "kagura-memory": {
      "type": "streamable-http",
      "url": "{server_url}/mcp",
      "headers": {
        "X-Workspace-ID": "{workspace_id}",
        "Authorization": "Bearer {api_key}"
      }
    }
  }
}
```

If `.mcp.json` already exists with other servers, merge the kagura-memory entry.

Add to `.gitignore` (contains API key):

```bash
grep -q '.mcp.json' .gitignore 2>/dev/null || echo '.mcp.json' >> .gitignore
```

Tell the user to restart Claude Code to pick up the config.

### 3. Show recommended workflows

**Solo developer — project knowledge base:**
1. Create one context per project
2. `remember` decisions, architecture choices, bug fixes as you work
3. `recall` at the start of each session to load context

**Team — shared knowledge base:**
1. Create shared contexts (Pro plan, `is_private: false`)
2. Team members connect via their own API keys (same workspace)
3. Onboarding: new members `recall` to learn project history

**External integration — file and vault sync:**
Use `source_uri` and `source_type` with `remember` to track where knowledge originated (e.g. Obsidian vaults, local files, web pages). Then use `source_uri_prefix` and `source_type` filters with `recall` to query memories from a specific source. Example: `remember(..., source_uri="vault://my-vault/note.md", source_type="vault")` → `recall(..., filters={"source_uri_prefix": "vault://my-vault/"})`.

**Session workflow:**
1. Start session: `/kagura-memory:session-start` to restore context
2. During work: `/kagura-memory:remember` and `/kagura-memory:recall` as needed
3. End session: `/kagura-memory:session-summary` to save learnings

### 4. Optional: SessionStart hook

To automatically remind yourself to restore session context, add this hook to your project's `.claude/settings.json`. Substitute `{server_url}` with the same URL you put in `.mcp.json` (e.g. `https://memory.kagura-ai.com` for the hosted service or `http://localhost:8080` for a local instance):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "curl -sf {server_url}/health >/dev/null 2>&1 && echo 'Kagura Memory Cloud is connected. Run /kagura-memory:session-start to restore previous session context.' || true"
          }
        ]
      }
    ]
  }
}
```

Merge this into your existing `hooks` object if you already have other hooks defined.

### 5. Show available plugin skills

| Skill | Description |
|-------|-------------|
| `session-start` | Restore previous session context |
| `session-summary` | Save session knowledge before ending |
| `recall` | Search past knowledge |
| `remember` | Save new knowledge |
| `guide` | This guide |
| `smoke-test` | Verify all MCP tools work |

### 6. Install in another project / machine

If you're setting up Kagura Memory Cloud plugin in a new project or on another machine:

**Option A: Marketplace install (recommended)**

Inside Claude Code, run:

```
/plugin install kagura-memory@kagura-memory-cloud
```

The argument is `<plugin-name>@<marketplace-name>` — the plugin is `kagura-memory` and it lives in the `kagura-memory-cloud` marketplace.

After install, the plugin skills (`/kagura-memory:*`) become available. Proceed to Step 2 above to configure the MCP server connection.

**Option B: Local install (from this repository)**

If you have the `memory-cloud` repo cloned locally, first add it as a local marketplace, then install:

```
/plugin marketplace add /path/to/memory-cloud
/plugin install kagura-memory@kagura-memory-cloud
```

This installs the plugin from `.claude-plugin/plugin.json` and `claude-skills/` in the repo.

**After either install:**
- Plugin skills are available immediately after restart
- MCP server connection still needs to be configured (Step 2) — the plugin provides skills, not the server connection
- Each project needs its own `.mcp.json` with workspace-specific credentials
