# Getting Started

This guide will help you get started with Kagura Memory Cloud.

## Prerequisites

- Docker and Docker Compose
- Python 3.11+ (for backend / CLI tools)
- Node.js 20+ (for frontend development, optional if using Docker)

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
```

### 2. Environment Setup

```bash
cp .env.example .env.local
```

Edit `.env.local` — the minimum required settings are already set for local development. OAuth providers (Google, GitHub) are **optional**.

### 3. Start Services

```bash
docker compose up -d
```

This will start:
- Backend API (port 8080)
- Frontend (port 3000)
- PostgreSQL (port 5432, loopback only)
- Qdrant (port 6333, loopback only)
- Redis (port 6379, loopback only)

The three data stores are published on `127.0.0.1` only — in this stack Redis and Qdrant run without authentication and PostgreSQL uses the default password from `.env.example`, so nothing off the machine should reach them. The `localhost` URLs in `.env.local` work unchanged. The API and frontend are the intended entry points and stay reachable from other hosts. See [Reaching the data stores](#reaching-the-data-stores) for the override.

### 4. Run Database Migrations

```bash
cd backend && alembic upgrade head
```

### 5. Create Admin Account

```bash
python -m src.cli.create_admin
```

This interactive command will:
- Create an admin user with login ID and password
- Set up MFA (TOTP) with your authenticator app (recommended)
- Create a personal workspace
- Generate an API key
- Write `.mcp.json` for MCP client configuration

### 6. Access the Application

- **Web UI**: http://localhost:3000
- **Admin Login**: Click "Admin Login" link on the login page
- **API Docs**: http://localhost:8080/docs
- **Health Check**: http://localhost:8080/health

### Reaching the data stores

On the machine that runs the stack, the published loopback ports and the compose network both work:

```bash
docker compose exec postgres psql -U kagura -d kagura
docker compose exec redis redis-cli
curl http://127.0.0.1:6333/collections
```

From another machine, forward the port over SSH to the dev host's loopback instead of opening it on the network:

```bash
ssh -L 5432:127.0.0.1:5432 dev-host    # then connect to localhost:5432 locally
```

If a remote client genuinely has to connect directly, `COMPOSE_BIND_HOST` sets the address the data-store ports are published on. Compose reads it from the shell or from a project `.env` file — not from `.env.local`:

```bash
COMPOSE_BIND_HOST=0.0.0.0 docker compose up -d   # every interface — the binding before #1626
```

Use a specific private address rather than `0.0.0.0` where you can, and do not use this override on a host with a public address. If a tool on your machine resolves `localhost` to `::1` only and does not fall back to IPv4, point it at `127.0.0.1` explicitly. On WSL2 the ports are published inside the distro; if a Windows-side tool cannot reach `localhost:5432`, the override above restores the previous binding.

**Upgrading from an earlier release:** setups that reached this stack's PostgreSQL, Qdrant or Redis from another machine must set `COMPOSE_BIND_HOST` or switch to the SSH forward. Anyone using `docker-compose.yml` as a deployment file with remote `psql` / Qdrant access has the same two options, or moves to the `terraform/single-server` layout, which publishes no data-store ports at all.

## Admin CLI Tools

All commands run from the `backend/` directory:

```bash
# Create admin (first time setup)
python -m src.cli.create_admin

# Reset password or disable/re-enable MFA
python -m src.cli.reset_password

# Delete admin (for re-creation)
python -m src.cli.delete_admin
```

> **Note**: Docker API container must be running. CLI reads `API_KEY_SECRET` from it.

## Authentication

Kagura Memory Cloud supports multiple authentication methods:

- **Password + MFA**: For the admin user (created via CLI)
- **Google OAuth**: Optional, configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env.local`
- **GitHub OAuth**: Optional, configure `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `.env.local`

## MCP Integration

The Web UI MCP Setup Guide at `/workspace/integrations/credentials?tab=api-keys` renders ready-to-paste snippets for each client below. The manual snippets in this section are the source of truth — keep them in sync if you change the supported transport shape.

### Which URL?

Every snippet below takes one of two endpoint URLs — same server, same API key:

- **All tools (default):** `http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID`
- **Core tools only — smaller tool list:** `http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID?profile=core`

Pick core when your client loads every tool schema at session start (it is about 65% smaller). It lists the 12 memory and context tools and leaves out Sleep, analyses, files, edges, secrets, resources and the agent control plane — those stay callable, they are just not listed; switch back to the default URL to see them. See [Tool Profiles](mcp-tools.md#tool-profiles) for the exact tool set. The snippets show the default URL; in the Web UI, the **Core tools only** switch above them writes `?profile=core` into every snippet.

### Claude Code / Claude Desktop

The `create_admin` CLI automatically generates `.mcp.json`. If you need to create it manually:

```json
{
  "mcpServers": {
    "kagura-memory": {
      "type": "http",
      "url": "http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

Core tools only: set `"url"` to `"http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID?profile=core"` instead ([which one?](#which-url)). The generated `.mcp.json` and `.mcp.json.example` both carry the default URL.

Restart Claude Code to pick up the config, then test with `remember` and `recall` tools.

The **kagura-memory** Claude Code plugin adds session skills and tool-guardrail hooks that deliver `details.tool_trigger` memories at the matching tool call — setup and the `?guardrails=off` URL rule are under "Claude Code Plugin › Tool guardrails (hooks)" in [MCP Client Setup](mcp-clients.md).

### Cursor

Cursor reads the same `mcpServers` shape as Claude Code. Add the snippet above to your Cursor settings file (Settings → MCP), then restart Cursor.

### ChatGPT (Custom Connectors)

ChatGPT custom connectors take a flatter shape — paste the URL and header into the connector form fields:

```
ChatGPT → Settings → Custom Connectors → New connector
  URL: http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID
  Authorization: Bearer YOUR_API_KEY
```

Core tools only: use `http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID?profile=core` as the URL instead ([which one?](#which-url)).

### Codex CLI

Checked against Codex `rust-v0.155.1`. Codex reads the API key from an environment variable that its config names — the key itself never goes into `~/.codex/config.toml`.

**Recommended — `codex mcp add`:**

```bash
export KAGURA_API_KEY="kagura_xxxxxxxxxxxx"
codex mcp add kagura-memory --url "http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID" --bearer-token-env-var KAGURA_API_KEY
```

`codex mcp list` now shows `kagura-memory` with `KAGURA_API_KEY` as its bearer token env var. Export `KAGURA_API_KEY` in every shell you start Codex from (or in your shell profile).

**Manual equivalent — `~/.codex/config.toml`:**

The command writes this entry; paste it yourself if you prefer editing the file:

```toml
[mcp_servers.kagura-memory]
url = "http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID"
bearer_token_env_var = "KAGURA_API_KEY"
```

These two keys only. Codex rejects an inline `bearer_token` on an HTTP server, and the whole `config.toml` then fails to load ([Troubleshooting](troubleshooting.md#codex-cli--bearer_token-is-not-supported-for-streamable_http)); `type` is not a Codex key.

Core tools only: set `url = "http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID?profile=core"` instead ([which one?](#which-url)).

Restart Codex CLI to pick up the config.

**Optional — the Kagura Memory skill:**

The `kagura-memory` plugin adds the Kagura Memory skill (session restore, recall and remember as natural-language requests) on top of the server configured above; it does not configure or sign in to the server. Register this repository's marketplace, then install the plugin:

```bash
codex plugin marketplace add https://github.com/kagura-ai/memory-cloud
codex plugin add kagura-memory@kagura-memory-cloud
```

The Codex marketplace file is `.agents/plugins/marketplace.json` (it points at `plugins/kagura-memory/`); the `.claude-plugin/marketplace.json` beside it is the Claude Code marketplace and, loaded into Codex, only registers idle hooks.

**Optional — tool guardrails:**

The plugin also bundles hooks (`hooks/hooks.json`) that deliver tool guardrails — memories marked with `details.tool_trigger` — at the matching tool call: `inform` adds the memory as context, `block` denies the call once with the memory as the reason ([contract](mcp-tools.md#tool-guardrails)). Nothing runs until you set them up:

1. Ask the skill to "turn on Kagura guardrails". It shows the `{"context_id": …, "max_action": "block"}` it will write to `config.json` in the plugin's data directory (`~/.codex/plugins/data/kagura-memory-*/`) and writes it after you confirm.
2. Add `?guardrails=off` to the `url` above so the server does not also send a guardrail digest.
3. Open `/hooks` in Codex and trust the kagura-memory hooks. Codex skips plugin-bundled hooks until each user trusts them, and asks again only when `hooks.json` changes.

The hooks read the `[mcp_servers.kagura-memory]` entry above (`bearer_token_env_var`, `env_http_headers` or `http_headers` — exactly one; never a project-level `.codex/config.toml`) and need `python3` 3.11+ on `PATH`. Windows, web and cloud tasks are not supported. If nothing happens, see [Troubleshooting](troubleshooting.md#codex-cli--kagura-memory-hooks-never-run).

## Quick API Test

```bash
export KAGURA_API_KEY="kagura_xxxxxxxxxxxx"

# Remember a memory
curl -X POST http://localhost:8080/api/v1/memory/remember \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Test memory",
    "content": "This is a test memory",
    "type": "note",
    "importance": 0.8
  }'

# Recall memories
curl -X POST http://localhost:8080/api/v1/memory/recall \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "test",
    "k": 5
  }'
```

## Next Steps

- [API Reference](api-reference.md) — Detailed API documentation
- [Architecture](architecture.md) — System design overview
- [Concepts](concepts.md) — Core concepts (contexts, workspaces, neural memory)
- [Sleep Maintenance](sleep-maintenance.md) — Background cleanup cycle, `sleep_mode`, observability, and rollback

## Troubleshooting

### Docker containers not starting

```bash
docker compose logs api
docker compose restart
```

### Database migration issues

```bash
cd backend && alembic upgrade head
```

### MFA locked out

```bash
cd backend && python -m src.cli.reset_password
# Choose option 2 (Disable MFA) or 3 (Both)
```

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **API Docs**: http://localhost:8080/docs
