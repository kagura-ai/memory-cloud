---
description: Show Kagura Memory Cloud setup and usage guide
---

Show the user a comprehensive guide for Kagura Memory Cloud. Cover all relevant sections based on their current state.

## Steps

### 1. Check current state

```bash
curl -sf http://localhost:8080/health >/dev/null 2>&1 && echo "API: running" || echo "API: not running"
ls .mcp.json 2>/dev/null && echo "MCP config: exists" || echo "MCP config: missing"
ls .env.local 2>/dev/null && echo "env: exists" || echo "env: missing"
```

### 2. Show relevant guide sections

Based on the state above, show the sections the user needs. Always show all sections as a reference, but highlight what needs attention.

#### OAuth Setup (Authentication)

Two providers supported:

- **Google OAuth2** (required):
  1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
  2. Create OAuth 2.0 Client ID (Web application)
  3. Add authorized redirect URI: `http://localhost:8080/api/v1/auth/google/callback`
  4. Set in `.env.local`:
     ```
     GOOGLE_CLIENT_ID=your_client_id
     GOOGLE_CLIENT_SECRET=your_client_secret
     ```

- **GitHub OAuth2** (optional):
  1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
  2. Create OAuth App
  3. Set callback URL: `http://localhost:8080/api/v1/auth/github/callback`
  4. Set in `.env.local`:
     ```
     GITHUB_CLIENT_ID=your_client_id
     GITHUB_CLIENT_SECRET=your_client_secret
     ```

#### API Key & MCP Setup

1. Log in at `http://localhost:3000`
2. Go to **Workspace > Integrations > API Keys** (`http://localhost:3000/workspace/integrations/api-keys`)
3. Create an API key (starts with `kagura_`)
4. Copy `.mcp.json.example` to `.mcp.json`:
   ```bash
   cp .mcp.json.example .mcp.json
   ```
5. Edit `.mcp.json` — set your workspace ID (from URL bar) and API key
6. Restart Claude Code to load MCP config

#### OpenAI API Key (for embeddings)

Required for memory storage and search:
1. Get an API key from [OpenAI](https://platform.openai.com/api-keys)
2. Set in `.env.local`:
   ```
   OPENAI_API_KEY=sk-...
   ```
   Or add as an External API Key in Web UI: **Workspace > Integrations > External Keys**

#### Resource Tokens (External Data Ingestion)

Resource Tokens allow external systems (CI/CD, webhooks, scripts) to push data into Kagura Memory Cloud via the Resource Event API. Pro plan only.

1. Go to **Workspace > Integrations > Resource Tokens** in Web UI
2. Create a token scoped to a specific `resource_id` (e.g., `github-issues`, `slack-messages`)
3. Use the token to push events:
   ```bash
   curl -X POST http://localhost:8080/api/v1/resources/events \
     -H "Authorization: Bearer rt_{your_token}" \
     -H "Content-Type: application/json" \
     -d '{"resource_id": "github-issues", "op": "upsert", "doc_id": "issue-123", "payload": {...}}'
   ```
4. Events are automatically indexed into memories via the Indexer

Quota limits per plan: S=0 tokens, M=3 tokens, L=30 tokens.

#### OAuth2 Server (App Credentials)

Kagura Memory Cloud can act as an OAuth2 authorization server, allowing third-party apps to access memories on behalf of users.

1. Go to **Workspace > Integrations > App Credentials** in Web UI
2. Register an OAuth2 client (get `client_id` and `client_secret`)
3. Implement the standard OAuth2 Authorization Code flow with PKCE:
   - Authorize: `GET /api/v1/oauth/authorize`
   - Token: `POST /api/v1/oauth/token`
   - Introspect: `POST /api/v1/oauth/introspect` (RFC 7662)

#### API Documentation

- **Swagger UI**: `http://localhost:8080/docs`
- **ReDoc**: `http://localhost:8080/redoc`
- **OpenAPI JSON**: `http://localhost:8080/openapi.json`

#### Plan Tiers & Quotas

| Plan | Contexts | Memories | MCP/day | REST/day | Members | Resource Tokens |
|------|----------|----------|---------|----------|---------|-----------------|
| S (Free) | 1 | 1,000 | 1,000 | — | 1 | — |
| M (Basic) | 3 | 10,000 | 10,000 | 1,000 | 1 | 3 |
| L (Pro) | 20 | 100,000 | 50,000 | 5,000 | 10 | 30 |

Self-hosted: assign L plan to your admin workspace. Override limits via env vars (`PLAN_FREE_MAX_CONTEXTS`, etc.).

**Addon quotas** (admin only): Increase quotas beyond plan defaults via **Admin > Plans** (`/admin/plans`). Expand a workspace row to see Base/Addon/Effective breakdown, then click **Edit Addons** to add extra memory, MCP quota, or member slots.

#### Memory Usage

If MCP is connected, call the `kagura_memory_usage_guide` MCP tool to show the full memory usage guide (remember, recall, forget, explore, etc.).

If MCP is not connected, direct the user to complete the MCP setup above first.

#### SaaS / Production Deployment

For running Kagura Memory Cloud as a hosted service:

1. Set production environment variables in `.env.local`:
   ```
   ENVIRONMENT=production
   DATABASE_URL=postgresql+asyncpg://user:pass@db-host:5432/kagura
   QDRANT_URL=http://qdrant-host:6333
   REDIS_URL=redis://redis-host:6379
   CORS_ORIGINS=https://your-domain.com
   SESSION_SECRET=<random-64-chars>
   ```

2. Enable Stripe billing for self-service plan upgrades:
   ```
   BILLING_ENABLED=true
   STRIPE_SECRET_KEY=sk_live_...
   STRIPE_WEBHOOK_SECRET=whsec_...
   STRIPE_PRICE_BASIC=price_xxx
   STRIPE_PRICE_PRO=price_yyy
   ```

3. Optional controls:
   ```
   ALLOW_REGISTRATION=true        # Open/closed registration
   GOOGLE_OAUTH_ENABLED=true      # Enable/disable Google login
   GITHUB_OAUTH_ENABLED=true      # Enable/disable GitHub login
   ```

Users sign up via OAuth, get a Free (S) workspace, and can upgrade to M/L via Stripe checkout. Admins manage users and plans via the Admin panel (`/admin`).

#### Recommended Workflows

**Solo developer — project knowledge base:**
1. Create one context per project (e.g., `my-app`, `infra`)
2. `remember` decisions, architecture choices, bug fixes as you work
3. `recall` at the start of each session to load context
4. Neural Memory auto-links related memories over time

**Team — shared knowledge base:**
1. Create shared contexts (Pro plan, `is_private: false`)
2. Team members connect via their own API keys (same workspace)
3. Onboarding: new members `recall` to learn project history
4. Code reviews: `recall` past decisions to understand "why"

**Claude Code power user — memory sync:**
1. Enable auto-sync hook (see MCP setup above) — Claude Code's local memories auto-upload
2. Use `/remember` and `/recall` slash commands for quick access
3. Use `explore()` after `recall()` to discover related knowledge via Neural Memory graph
4. Set `usage_guide` on each context to teach the AI your project's conventions

**CI/CD integration — automated knowledge capture:**
1. Create Resource Tokens for `github-issues`, `slack-threads`, etc.
2. Push events via Resource Event API from webhooks/pipelines
3. Indexer converts events into searchable memories automatically
4. Team can `recall` across code, issues, and discussions in one search

### 3. Suggest next steps

Based on what's missing, suggest the logical next action (e.g., "Run `/setup` to start services", "Create an API key", "Set up OAuth credentials").
