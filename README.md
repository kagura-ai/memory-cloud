<p align="center">
  <a href="https://www.kagura-ai.com">
    <img src="docs/assets/kagura-logo.svg" alt="Kagura Memory Cloud" width="400">
  </a>
</p>

<p align="center">
  English · <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <strong>Self-hosted LLM Knowledge Base for teams</strong> — beyond RAG.<br>
  MCP-as-compile-API + Hebbian learning + Sleep Maintenance.<br>
  <a href="https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f">Karpathy's LLM Wiki pattern</a>, scaled for teams and persistence.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <a href="https://github.com/kagura-ai/memory-cloud/actions/workflows/ci.yml"><img src="https://github.com/kagura-ai/memory-cloud/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://codecov.io/gh/kagura-ai/memory-cloud"><img src="https://codecov.io/gh/kagura-ai/memory-cloud/graph/badge.svg" alt="codecov"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/node.js-20+-green.svg" alt="Node.js 20+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Streamable_HTTP-purple.svg" alt="MCP"></a>
  <a href="https://safeskill.dev/scan/kagura-ai-memory-cloud"><img src="https://img.shields.io/badge/SafeSkill-90%2F100_Verified%20Safe-brightgreen" alt="SafeSkill 90/100"></a>
</p>

<p align="center">
  Works with Claude, ChatGPT, Gemini, and any MCP-compatible client.<br>
  <a href="https://github.com/kagura-ai/kagura-memory-python-sdk"><strong>Python SDK (KaguraClient / KaguraAgent)</strong></a>
</p>

## Why Kagura Memory Cloud?

> **Your AI forgets everything after each conversation. Kagura fixes that — and gets smarter every time you search.**

Most AI memory tools are just vector databases with a chat wrapper. Kagura is different — it implements the full **LLM Knowledge Base** pattern (Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)) at team scale:

| Approach | Storage | Compounding | Scale |
|---|---|---|---|
| Vector DB / RAG | Embedded chunks | None — retrieve-only | Any |
| Karpathy's LLM Wiki | Markdown files | LLM rewrites pages | Personal (~100 pages) |
| **Kagura Memory Cloud** | **PostgreSQL + Qdrant + Neural graph** | **Hebbian + Sleep Maintenance** | **Team / org** |



| Feature | Description |
|---------|-------------|
| **Adaptive Memory** | Every search automatically strengthens connections between related memories. The more you use it, the better `explore()` discovers hidden relationships. |
| **Hybrid Search** | Semantic (OpenAI/Ollama) + BM25 keyword — 96% top-1 accuracy |
| **AI Reranking** | Ollama (local, free), Voyage AI, or Cohere — cross-encoder reranking for precision |
| **Neural Memory Graph** | Hebbian learning builds a knowledge graph in the background. `explore()` traverses it for serendipitous discovery. |
| **37 MCP Tools** | Memory, Neural edges, Contexts, Tags, Files (R2), Analyses (broadlistening), Resources, Sleep Maintenance, Usage |
| **Multi-Provider** | OpenAI or Ollama (local, private, zero cost) for embeddings |
| **Team Ready** | Workspaces, RBAC, context isolation, shared memory |
| **Web UI** | Next.js dashboard — contexts, search settings, member management |
| **5-Minute Setup** | `./setup.sh` and you're done |

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

### LLM Knowledge Base — 5-Layer Implementation

Karpathy's [LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) describes a 5-layer "living knowledge base" — beyond traditional RAG. Kagura implements all 5 layers at team scale:

| Layer | Kagura Implementation | Difference from Karpathy's pattern |
|---|---|---|
| **Ingest** | REST `/api/v1/memory`, MCP `remember`, R2 file storage, resource tokens | + binary blobs, + multi-tenant |
| **Compile** | **MCP-as-compile-API** — chat agent compiles via structured tool calls (`remember(summary, content, type, tags)`) + Sleep Maintenance for batch consolidation | Continuous micro-compile (not batch wiki rewrite) — schema-enforced output |
| **Index** | Triple index: **BM25** (keyword) + **Qdrant** (semantic) + **Hebbian graph** (relational) — all auto-maintained | No manual `index.md` upkeep |
| **Query** | Hybrid Search + AI Reranker + `explore` graph traversal | Beyond markdown grep — supports semantic + relational queries |
| **Enhance** | **Hebbian learning** — every `recall()` strengthens edges between co-retrieved memories. Sleep Maintenance consolidates periodically. | Background graph evolution (zero LLM cost) vs LLM-driven page rewrites |

**Compounding loop**: Currently explicit (user/agent calls `remember()` after synthesizing answers). Auto-write-back of synthesized answers is intentionally opt-in to keep noise low.


### Adaptive Memory: Two Search Paths

Kagura separates **precision search** and **discovery** into two independent paths, each optimized for its purpose:

```
recall()  ──→ Hybrid Search (semantic + BM25) ──→ [Reranker] ──→ Precise results
                      │
                      └──→ Hebbian Learning (background) ──→ Graph edges grow
                                                                │
explore() ──→ Graph Traversal (Neural Memory) ←─────────────────┘  Related discoveries
```

- **`recall()`** — Precision search. Hybrid (semantic 60% + BM25 40%) with optional AI reranking. Returns the most relevant memories.
- **`explore()`** — Discovery. Traverses the Neural Memory graph to find related memories that keyword search would miss.
- **Hebbian learning** — Every `recall()` silently strengthens edges between co-retrieved memories. No explicit training needed — the graph grows organically as you use the system.

This separation is intentional: mixing graph signals into recall degrades precision ([validated via benchmarks](docs/neural-memory-evaluation.md)). Instead, each path does what it's best at.

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
- OpenAI API key (for embeddings) — or Ollama for local embeddings
- OAuth2 credentials (optional — password + MFA login available without OAuth)

### Setup

**One-line setup:**

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
./setup.sh
```

**With Claude Code:**

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
claude   # then run /setup
```

**Step-by-step setup:**

```bash
# 1. Clone
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud

# 2. Configure environment (generates secrets, prompts for API keys)
(cd backend && python3 -m src.cli.setup_env)

# 3. Start all services
docker compose up -d

# 4. Run migrations
(cd backend && alembic upgrade head)

# 5. Create admin account (interactive — sets password, MFA, API key, embedding provider)
(cd backend && python3 -m src.cli.create_admin)

# Backend API:  http://localhost:8080
# Frontend UI:  http://localhost:3000
# API docs:     http://localhost:8080/redoc
```

**`.env.local` settings** (auto-configured by `setup_env`):

| Setting | Required | Description |
|---------|----------|-------------|
| `API_KEY_SECRET` | **Yes** | Secret for API key encryption (auto-generated) |
| `JWT_SECRET` | **Yes** | Secret for JWT tokens (auto-generated) |
| `OPENAI_API_KEY` | **Yes**\* | OpenAI API key for embeddings |
| `OLLAMA_BASE_URL` | No | Ollama URL (default: `http://localhost:11434`) |
| `EMBEDDING_PROVIDER` | No | `openai` (default) or `ollama` |
| `GOOGLE_CLIENT_ID/SECRET` | No | Google OAuth2 login (optional — password login available) |
| `GITHUB_CLIENT_ID/SECRET` | No | GitHub OAuth2 login (optional) |

\* Either `OPENAI_API_KEY` or a running Ollama instance is required for memory features.

### Admin CLI

| Command | Purpose |
|---------|---------|
| `python3 -m src.cli.setup_env` | Generate secrets + configure `.env.local` (run before Docker) |
| `python3 -m src.cli.create_admin` | Create admin + workspace + API key + `.mcp.json` + embedding setup |
| `python3 -m src.cli.reset_password` | Reset password and/or MFA |
| `python3 -m src.cli.delete_admin` | Delete admin (for re-creation) |

> Run from `backend/` directory. Docker API container must be running.

<details>
<summary>Platform-specific notes</summary>

- **WSL (Windows)**: Install Docker Desktop for Windows and enable WSL integration
- **macOS**: Install Docker Desktop for Mac. `brew install python@3.11 node`
- **Linux (Ubuntu/Debian)**: `sudo apt install docker.io docker-compose-v2 python3.11 nodejs npm`
- **GCP (Production)**: Set production values in `.env.local` (`DATABASE_URL`, `QDRANT_URL`, `ENVIRONMENT=production`, `CORS_ORIGINS`)
- **Frontend env vars**: Copy `frontend/.env.example` to `frontend/.env.local` and set:
  - `NEXT_PUBLIC_API_URL` — backend URL (default: `http://localhost:8080`)
  - `NEXT_PUBLIC_APP_URL` — frontend URL for metadata
  - `NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME` / `BASIC` / `PRO` — plan display name customization (default: S/M/L)

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

37 tools across 9 categories. Workspace roles: **Owner** > Admin > Member > **Viewer** (read-only). Context roles: **Owner** > Editor > Viewer. Private contexts are visible only to the creator. Members may be restricted to specific contexts via allowlist.

### Memory (6)

| Tool | Description | Required Role |
|------|------------|---------------|
| `remember` | Store a new memory (summary + content + type) | Member+ |
| `recall` | Search memories with Hybrid Search | Viewer+ |
| `reference` | Get full 3-layer details of a memory | Viewer+ |
| `update_memory` | Update an existing memory in-place or upsert by external ID | Member+ |
| `forget` | Soft-delete a memory (30-day retention) | Member+ |
| `explore` | Discover related memories via Neural Memory graph | Viewer+ |

### Neural Edges (4)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_edges` | List edges connected to a memory | Viewer+ |
| `create_edge` | Create an edge between two memories | Member+ |
| `update_edge` | Update edge weight or type | Member+ |
| `delete_edge` | Delete an edge between two memories | Member+ |

### Contexts (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_context_info` | Get context metadata and guidelines | Viewer+ |
| `list_contexts` | List available contexts in workspace | Viewer+ |
| `create_context` | Create a new context | Owner/Admin |
| `update_context` | Update context settings (summary, usage guide, resource_id, is_public) | Editor+ |
| `delete_context` | Delete a context and all its memories | Owner/Admin |
| `merge_contexts` | Merge memories from source context into target context | Owner/Admin |
| `update_search_config` | Tune hybrid search weights and reranker settings per context | Editor+ |

### Tags (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_tags` | List tag vocabulary in a context (call before remember/recall to align tagging) | Viewer+ |

### Files / R2 Attachments (5)

| Tool | Description | Required Role |
|------|------------|---------------|
| `init_file_upload` | Reserve quota + return presigned PUT URL (R2, ≤100 MiB) | Member+ |
| `complete_file_upload` | Finalize upload after R2 PUT, verify sha256, mark as uploaded | Member+ |
| `list_files` | List uploaded, non-deleted files in the workspace (newest first) | Viewer+ |
| `get_file_download_url` | Issue presigned GET URL for a file | Viewer+ |
| `delete_file` | Soft-delete a file object | Member+ |

### Analyses — Broadlistening (5)

Cluster memories into themes (kouchou-ai-style UMAP + KMeans + LLM labeling) for large-scale qualitative analysis.

| Tool | Description | Required Role |
|------|------------|---------------|
| `analyze_context` | Start an analysis run (or preview cost with `dry_run=true`) | Owner + Pro plan + BYOK + quota |
| `list_analyses` | List past analysis runs for a context | Owner |
| `get_analysis` | Get a completed analysis (clusters, labels, stats) | Owner |
| `get_active_analysis` | Get the in-flight analysis for a context (if any) | Owner |
| `get_cluster` | Drill into a single cluster's member memories | Owner |

### Resources — External Data Ingestion (5)

| Tool | Description | Required Role |
|------|------------|---------------|
| `setup_resource` | Create public context + issue resource token | Owner/Admin + Pro plan |
| `list_resource_tokens` | List active resource tokens for your workspace | Owner/Admin |
| `ingest_events` | Batch upsert/delete events into a resource (max 100 events; session-auth MCP variant) | Member+ |
| `get_resource_impact` | Resource stats (tokens, memories, schema version) | Viewer+ |
| `get_resource_schema` | Field definitions for a resource | Viewer+ |

### Sleep Maintenance (3)

Background consolidation of memories (decay, edge pruning, theme summarization).

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_sleep_history` | List past sleep runs | Viewer+ |
| `get_sleep_report` | Detailed sleep report with all actions | Viewer+ |
| `rollback_sleep_run` | Rollback all actions from a completed sleep run | Member+ (report owner only) |

### Usage (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_usage` | Get current workspace usage (memories, contexts, members, MCP calls/day) | Viewer+ |

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

**API reference** — two complementary entry points:

- **Concepts (markdown)**: [API Reference](docs/api-reference.md) — auth, base URLs, MCP endpoint, request/response examples
- **Endpoint reference (live)**: `http://localhost:8080/redoc` — auto-generated from FastAPI, always in sync with the running backend

**Concepts & guides:**

- [Core Concepts](docs/concepts.md) — Workspace, Context, Memory, Neural Memory, MCP Tools
- [Architecture](docs/architecture.md) — System design and data flow
- [Getting Started](docs/getting-started.md) — Detailed setup guide
- [Chunking Guide](docs/chunking-guide.md) — Best practices for memory storage
- [Resource Tokens Guide](docs/resource-tokens-guide.md) — External data ingestion via resource tokens
- [Neural Memory Evaluation](docs/neural-memory-evaluation.md) — Benchmark results, architecture decisions
- [Search Quality Benchmark](docs/search-quality-benchmark.md) — Accuracy tests, reranking, best practices
- [Deployment](docs/deployment.md) — Production deployment with Caddy reverse proxy
- [Contributing](CONTRIBUTING.md) — Development setup, code style, PR workflow
- [Security](SECURITY.md) — Vulnerability reporting, security design
- [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk) — `KaguraClient` and `KaguraAgent` for Python
- **Project site**: [www.kagura-ai.com](https://www.kagura-ai.com) — overview, use cases, getting started paths

## 🇯🇵 日本語ガイド / Japanese Guide

<details>
<summary>Claude Code プラグインの使い方</summary>

### インストール

```bash
# マーケットプレイスから追加
/plugin marketplace add kagura-ai/memory-cloud
/plugin install kagura-memory@kagura-memory-cloud
```

### 利用可能なスキル

| スキル | 説明 |
|--------|------|
| `/kagura-memory:session-start` | 前回のセッションコンテキストを復元 |
| `/kagura-memory:session-summary` | セッション終了前に知識を保存 |
| `/kagura-memory:recall` | 過去の知識を検索 |
| `/kagura-memory:remember` | 新しい知識を保存 |
| `/kagura-memory:guide` | 使い方ガイド・接続確認・セットアップ |
| `/kagura-memory:smoke-test` | 全MCPツールの動作確認 |

### 推奨ワークフロー

```
/kagura-memory:session-start       # ← ここから：前回のコンテキストを復元
  ... 通常の作業 ...
/kagura-memory:recall              # 過去の設計判断・パターン・修正を検索
/kagura-memory:remember            # 重要な学びを都度保存
  ... 作業完了 ...
/kagura-memory:session-summary     # ← ここで終了：次回のためにセッション知識を保存
```

スキルは MCP ツール（`recall`, `remember` など）をワークフローロジックで包んだものです。コンテキスト選択、git 状態の分析、構造化プロンプトが組み込まれています。セッション管理やガイド付きワークフローにはスキルを、細かい操作には MCP ツールを直接使ってください。

> **前提条件:** MCP接続の設定が必要です（`.mcp.json` に API キーを設定）。プロジェクトで `/kagura-memory:guide` を実行してセットアップしてください。

</details>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and PR workflow.

## License

[Apache License 2.0](LICENSE)
