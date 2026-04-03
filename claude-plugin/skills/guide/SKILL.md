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

**Session workflow:**
1. Start session: `/kagura-memory:session-start` to restore context
2. During work: `/kagura-memory:remember` and `/kagura-memory:recall` as needed
3. End session: `/kagura-memory:session-summary` to save learnings

### 4. Show available plugin skills

| Skill | Description |
|-------|-------------|
| `session-start` | Restore previous session context |
| `session-summary` | Save session knowledge before ending |
| `recall` | Search past knowledge |
| `remember` | Save new knowledge |
| `guide` | This guide |
| `smoke-test` | Verify all MCP tools work |
