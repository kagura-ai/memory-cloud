# Kagura Memory Cloud

**Universal AI Memory Platform** — Self-hosted, open source.

Give your AI assistants persistent memory across conversations. Works with Claude, ChatGPT, Gemini, and any MCP-compatible client.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![CI](https://github.com/kagura-ai/memory-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/kagura-ai/memory-cloud/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/kagura-ai/memory-cloud/graph/badge.svg)](https://codecov.io/gh/kagura-ai/memory-cloud)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Node.js 20+](https://img.shields.io/badge/node.js-20+-green.svg)](https://nodejs.org/)
[![MCP](https://img.shields.io/badge/MCP-Streamable_HTTP-purple.svg)](https://modelcontextprotocol.io/)
[![Python SDK](https://img.shields.io/badge/SDK-kagura--memory-orange.svg)](https://github.com/kagura-ai/kagura-memory-python-sdk)

## What is Kagura Memory Cloud?

Kagura Memory Cloud is a memory system that lets AI assistants **remember, search, and learn** from past conversations. It provides:

- **10 MCP Tools** — remember, recall, forget, reference, explore, get_context_info, list_contexts, create_context, update_context, usage_guide
- **3-Layer Memory Architecture** — summary (search-optimized) / context / full content
- **Hybrid Search** — 60% semantic (OpenAI embedding) + 40% full-text (Qdrant BM25)
- **Neural Memory** — Hebbian learning creates automatic connections between related memories
- **Team Collaboration** — Workspaces with RBAC (Owner/Admin/Member/Viewer)
- **Web Management UI** — Next.js dashboard for browsing, searching, and managing memories

## Architecture

```
Workspace (team/org)
├── Context A ("my-project")     ← like a folder
│   ├── Memory 1                 ← 3-layer: summary / context / content
│   ├── Memory 2
│   └── Neural edges (Hebbian)   ← automatic connections
├── Context B ("learning-notes")
│   └── ...
└── Members (Owner/Admin/Member/Viewer)
```

**Search pipeline:**

```
Query → OpenAI Embedding → Qdrant Hybrid Search (semantic 60% + BM25 40%)
                         → Optional AI Reranking
                         → Neural Memory boosting
                         → Results (ranked by relevance)
```

**Data isolation:** All data is filtered by `workspace_id → context_id → user_id`. Memories never leak across boundaries. Single Qdrant collection with payload filtering.

**Tech stack:** FastAPI (async) · PostgreSQL · Qdrant · Redis · Next.js 16 · OAuth2 · MCP over Streamable HTTP

## Quick Start

### System Requirements

|  | Minimum | Recommended |
|--|---------|-------------|
| CPU | 2 cores | 4+ cores |
| RAM | 4 GB | 8+ GB |
| Disk | 10 GB free | 20+ GB free |

### Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Node.js 20+
- Google OAuth2 credentials ([console.cloud.google.com](https://console.cloud.google.com/apis/credentials))
- GitHub OAuth2 credentials (optional — [github.com/settings/developers](https://github.com/settings/developers))
- OpenAI API key (for embeddings)

### Setup

**With Claude Code** (recommended):

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
cp .env.example .env.local
# Edit .env.local — set GOOGLE_CLIENT_ID/SECRET (and optionally GITHUB_CLIENT_ID/SECRET)
claude   # then run /setup
```

**Manual setup:**

```bash
# 1. Clone and configure
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
cp .env.example .env.local
# Edit .env.local — set GOOGLE_CLIENT_ID/SECRET (and optionally GITHUB_CLIENT_ID/SECRET)

# 2. Start all services
docker compose up -d

# 3. Run migrations
cd backend && alembic upgrade head

# 4. (Optional) Start frontend/backend in dev mode
cd backend && pip install -e ".[dev]" && uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
cd frontend && npm install && npm run dev

# Backend API:  http://localhost:8080
# Frontend UI:  http://localhost:3000
# Swagger docs: http://localhost:8080/docs
```

<details>
<summary>Platform-specific notes</summary>

- **WSL (Windows)**: Install Docker Desktop for Windows and enable WSL integration
- **macOS**: Install Docker Desktop for Mac. `brew install python@3.11 node`
- **Linux (Ubuntu/Debian)**: `sudo apt install docker.io docker-compose-v2 python3.11 nodejs npm`
- **GCP (Production)**: Set production values in `.env.local` (`DATABASE_URL`, `QDRANT_URL`, `ENVIRONMENT=production`, `CORS_ORIGINS`)

</details>

## MCP Client Setup

### Claude Code (Recommended)

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

### Claude Desktop / Claude Chat (Web)

**Claude Desktop**: Same `.mcp.json` format as Claude Code — place in your project root or `~/.claude/.mcp.json`.

**Claude Chat (claude.ai)**: Add as a remote MCP server in Settings > Integrations:
1. Click "Add Integration" → "Custom MCP Server"
2. Enter the MCP endpoint URL: `https://your-domain.com/mcp/w/{workspace_id}`
3. Add the `Authorization: Bearer kagura_{your_api_key}` header

> Claude Chat requires a publicly accessible URL (not `localhost`). Use a production deployment or tunnel (e.g., ngrok, Cloudflare Tunnel).

### ChatGPT Desktop

ChatGPT desktop app supports MCP servers. Add via Settings > MCP Servers:
1. Server URL: `https://your-domain.com/mcp/w/{workspace_id}`
2. Authentication: Bearer token `kagura_{your_api_key}`

> Like Claude Chat, ChatGPT requires a public URL. For local development, use a tunnel or the REST API directly.

### Gemini CLI

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

### Other MCP Clients / REST API

Any MCP-compatible client can connect via Streamable HTTP. For clients without MCP support, use the REST API directly:

```bash
# Search memories
curl -X POST -H "Authorization: Bearer kagura_{your_key}" \
  -H "Content-Type: application/json" \
  -d '{"query": "your search", "context_id": "..."}' \
  http://localhost:8080/api/v1/memories/search
```

## MCP Tools

| Tool | Description | Required Role |
|------|------------|---------------|
| `remember` | Store a new memory (summary + content + type) | Member+ |
| `recall` | Search memories with Hybrid Search | Viewer+ |
| `reference` | Get full 3-layer details of a memory | Viewer+ |
| `forget` | Soft-delete a memory (30-day retention) | Member+ |
| `explore` | Discover related memories via Neural Memory graph | Viewer+ |
| `get_context_info` | Get context metadata and guidelines | Viewer+ |
| `list_contexts` | List available contexts in workspace | Viewer+ |
| `create_context` | Create a new context | Owner/Admin |
| `update_context` | Update context settings (summary, usage guide) | Editor+ (Owner for summary/usage_guide) |
| `kagura_memory_usage_guide` | Get the usage guide | — |

Workspace roles: **Owner** > Admin > Member > **Viewer** (read-only). Context roles: **Owner** > Editor > Viewer. Private contexts are visible only to the creator. Members may be restricted to specific contexts via allowlist.

## REST API

In addition to MCP tools, a full REST API is available:

- **Memory**: remember, recall, reference, forget, explore (`/api/v1/memory/*`)
- **Contexts**: CRUD, search settings (`/api/v1/contexts/*`)
- **Attachments**: File upload/download for memories (`/api/v1/attachments/*`, 5MB limit)
- **Workspaces**: Management, members, invitations (`/api/v1/workspaces/*`)
- **Admin**: Users, plan management, neural config (`/api/v1/admin/*`)

Full API documentation: `http://localhost:8080/redoc`

## Authentication

Two OAuth2 providers are supported:

- **Google OAuth2** — Required. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
- **GitHub OAuth2** — Optional. Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`

Users with the same email address across providers share a single account.

## Plan Tier Customization

Plans control resource limits per workspace. Defaults:

| Plan | Contexts | Memories | MCP calls/day |
|------|----------|----------|---------------|
| S (Free) | 1 | 1,000 | 1,000 |
| M (Basic) | 3 | 10,000 | 10,000 |
| L (Pro) | 20 | 100,000 | 50,000 |

Override via environment variables:

```bash
PLAN_FREE_MAX_CONTEXTS=5
PLAN_FREE_MEMORY_LIMIT=5000
PLAN_BASIC_MAX_CONTEXTS=10
PLAN_PRO_MAX_CONTEXTS=50
```

For self-hosted single-user setups, assign the L (Pro) plan to your workspace. Plan changes are **admin-only** by default. For SaaS deployments with self-service billing, enable Stripe:

```bash
BILLING_ENABLED=true
STRIPE_SECRET_KEY=sk_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_BASIC=price_xxx
STRIPE_PRICE_PRO=price_yyy
```

## Development with Claude Code

This project is designed to be developed **with** Claude Code and Kagura Memory Cloud itself.

### Getting Started with Claude Code

1. [Install Claude Code](https://docs.anthropic.com/en/docs/claude-code)
2. Clone the repo and start services (see [Quick Start](#quick-start))
3. Create an API key in the Web UI (`http://localhost:3000`)
4. Create `.mcp.json` in the project root (see [Claude Code setup](#claude-code--claude-desktop))

   > `.mcp.json` is in `.gitignore` — never commit it (contains API keys)

5. Verify in Claude Code:
   ```
   > list_contexts()
   > create_context(name="my-project")
   ```
6. Pre-configured tooling loads automatically from `.claude/`:

### Slash Commands

| Command | Description |
|---------|-------------|
| `/setup` | Set up dev environment from scratch (detects WSL/macOS/Linux) |
| `/issue-start <number>` | Start work on a GitHub issue (branch + past knowledge recall) |
| `/remember <text>` | Save patterns, decisions, or learnings to memory |
| `/recall <query>` | Search past knowledge for relevant patterns |
| `/guide` | Show Kagura Memory Cloud usage guide |
| `/test` | Run full test suite (backend pytest + frontend build) |
| `/quality` | Run all quality checks (ruff, pyright, frontend build) |
| `/self-review` | Pre-PR self-review against SOLID/DRY/KISS/security checklist |
| `/self-maint` | Audit `.claude/` config against current codebase state |
| `/api-docs-audit` | Audit OpenAPI tags and endpoint documentation |

### Hooks (Automatic Safety Guards)

- **Auto-format**: Python (ruff) / TypeScript (prettier) on every file save
- **Secret detection**: Blocks commits containing hardcoded API keys or passwords
- **SQL injection prevention**: Blocks f-string SQL queries (enforces SQLAlchemy)
- **.env protection**: Prevents accidental modification of .env files
- **Migration protection**: Prevents modification of lock files
- **Memory sync**: Auto-syncs Claude Code memory files to Kagura Memory Cloud ([details](#claude-code-recommended))

### Agents

- **code-reviewer**: Read-only code review against project standards (runs on Sonnet)
- **test-runner**: Test execution, failure diagnosis, and auto-fix (runs on Sonnet)

### Rules (Auto-loaded Context)

- `rules/backend.md`: FastAPI/Python patterns, async requirements, testing conventions
- `rules/frontend.md`: Next.js/TypeScript patterns, Tailwind, SWR conventions
- `rules/security.md`: Auth requirements, RBAC, SQL safety, CORS policy

## Documentation

- [Core Concepts](docs/concepts.md) — Workspace, Context, Memory, Neural Memory, MCP Tools
- [Architecture](docs/architecture.md) — System design and data flow
- [Getting Started](docs/getting-started.md) — Detailed setup guide
- [API Reference](docs/api-reference.md) — REST API documentation
- [Chunking Guide](docs/chunking-guide.md) — Best practices for memory storage
- [Contributing](CONTRIBUTING.md) — Development setup, code style, PR workflow
- [Security](SECURITY.md) — Vulnerability reporting, security design
- [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk) — `KaguraClient` and `KaguraAgent` for Python

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and PR workflow.

## License

[Apache License 2.0](LICENSE)
