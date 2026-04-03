---
description: Show Kagura Memory Cloud usage guide and connection status
---

Show the user how to use Kagura Memory Cloud, and check their current connection status.

## Steps

### 1. Check MCP connection

```
kagura_memory_usage_guide()
```

If this succeeds, the MCP server is connected. Show the guide content returned.

If this fails, the MCP server is not connected. Guide the user through connection setup (Step 2).

### 2. MCP connection setup (if not connected)

Tell the user they need:

1. **A running Kagura Memory Cloud instance** (self-hosted or cloud)
2. **An API key** from the Kagura Memory Cloud web UI (Workspace > Integrations > API Keys)
3. **MCP client configuration** — add to `.mcp.json` in their project root:
   ```json
   {
     "mcpServers": {
       "kagura-memory": {
         "type": "streamable-http",
         "url": "http://localhost:8080/mcp",
         "headers": {
           "X-Workspace-ID": "<your-workspace-id>",
           "Authorization": "Bearer <your-api-key>"
         }
       }
     }
   }
   ```
4. Restart Claude Code to pick up the MCP config

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
