---
description: Set up Kagura Memory Cloud MCP connection for this project
---

Connect this project to a Kagura Memory Cloud instance via MCP.

## Steps

### 1. Check if already connected

```
list_contexts()
```

If this succeeds, MCP is already connected. Show the available contexts and exit.

### 2. Check for existing config

```bash
cat .mcp.json 2>/dev/null || echo "No .mcp.json found"
```

### 3. Gather connection details

Ask the user for:
- **Server URL**: Where their Kagura Memory Cloud instance is running (default: `http://localhost:8080`)
- **Workspace ID**: Found in the web UI URL bar after login
- **API key**: Created at Workspace > Integrations > API Keys (starts with `kagura_`)

### 4. Write MCP config

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

### 5. Add .mcp.json to .gitignore

```bash
grep -q '.mcp.json' .gitignore 2>/dev/null || echo '.mcp.json' >> .gitignore
```

The config contains an API key and should not be committed.

### 6. Verify connection

Tell the user to restart Claude Code, then test:

```
list_contexts()
```

If successful, suggest creating a context for their project:

```
create_context(name="{project-name}", description="{brief project description}")
```

### 7. Next steps

- Run `/kagura-memory:guide` for usage tips
- Run `/kagura-memory:smoke-test` to verify all tools work
