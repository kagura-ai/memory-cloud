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
- PostgreSQL (port 5432)
- Qdrant (port 6333)
- Redis (port 6379)

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

Restart Claude Code to pick up the config, then test with `remember` and `recall` tools.

### Cursor

Cursor reads the same `mcpServers` shape as Claude Code. Add the snippet above to your Cursor settings file (Settings → MCP), then restart Cursor.

### ChatGPT (Custom Connectors)

ChatGPT custom connectors take a flatter shape — paste the URL and header into the connector form fields:

```
ChatGPT → Settings → Custom Connectors → New connector
  URL: http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID
  Authorization: Bearer YOUR_API_KEY
```

### Codex CLI

**Recommended — plugin install (handles sign-in):**

```bash
codex plugin install kagura-memory@kagura-memory-cloud
```

The plugin adds `/recall`, `/remember`, and `/session-start` slash skills to Codex CLI.

**Manual fallback — `~/.codex/config.toml`:**

If the plugin install does not auto-configure the MCP server endpoint, add the snippet manually:

```toml
[mcp_servers.kagura-memory]
type = "http"
url = "http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID"
bearer_token = "YOUR_API_KEY"
```

Restart Codex CLI to pick up the config.

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
