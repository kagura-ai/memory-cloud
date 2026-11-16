# Getting Started

Set up a local Memory Cloud server, connect an AI client, and save your first memory. For the hosted service, use the [cloud connection guide](https://www.kagura-ai.com/help/connect/); registration is currently invite-only via the [waitlist](https://www.kagura-ai.com/waitlist/).

## Prerequisites

- Docker with Docker Compose, running before setup
- Python 3.11+ (`python3` on PATH)
- Bash on Linux/macOS or Windows with WSL2; `curl` if setup needs to install its pinned `uv`
- An OpenAI API key or a self-hosted embedding server reachable by the API container
- Node.js 24+ only for frontend development outside Docker

The root Compose stack is a development environment. Use [Deployment](deployment.md) for a public server.

## Recommended setup

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
bash setup.sh
```

The script generates configuration secrets, installs the pinned `uv` and locked backend dependencies plus the Kagura CLI, starts Docker services, runs migrations **inside the API container**, and creates an admin account interactively. It copies the generated `.mcp.json` to the repository root for Claude Code. Keep that file private: it contains an API key.

After setup, continue at [Access the application](#6-access-the-application). The manual path below is an alternative, not an additional set of steps to run afterwards.

## Installation

For a manual Docker setup, run the following from the repository root unless stated otherwise. The Python environment and SDK installation in `setup.sh` are useful for development but are not required for these container-based migration and admin commands.

### 1. Clone the Repository

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
```

### 2. Environment Setup

```bash
(cd backend && python3 -m src.cli.setup_env)
```

This generates `.env.local` with server secrets. Configure embeddings before using memory features:

| Provider | Configuration |
|---|---|
| OpenAI | Set `OPENAI_API_KEY` when prompted; the admin setup registers it for the workspace. |
| Self-hosted | Set `EMBEDDING_PROVIDER=self_hosted` and `SELF_HOSTED_BASE_URL` in `.env.local`, and make the embedding service reachable from the API container. |

Inside a container, `localhost` means that container, not the host running your embedding server. See [Deployment](deployment.md) for provider configuration. OAuth providers (Google, GitHub) are **optional**.

### 3. Start Services

```bash
docker compose up -d postgres qdrant redis
docker compose up -d api web
docker compose ps
```

Wait for the database and API health checks to pass before continuing. These commands start:
- Backend API (port 8080)
- Frontend (port 3000)
- PostgreSQL (port 5432, loopback only)
- Qdrant (port 6333, loopback only)
- Redis (port 6379, loopback only)

The three data stores are published on `127.0.0.1` only — in this stack Redis and Qdrant run without authentication and PostgreSQL uses the default password from `.env.example`, so nothing off the machine should reach them. The `localhost` URLs in `.env.local` work unchanged. The API and frontend are the intended entry points and stay reachable from other hosts. See [Reaching the data stores](#reaching-the-data-stores) for the override.

### 4. Run Database Migrations

```bash
docker compose exec api bash -c 'cd /app && PYTHONPATH=src alembic upgrade head'
```

### 5. Create Admin Account

```bash
docker compose exec -e MCP_JSON_DIR=/app api python3 -m src.cli.create_admin
docker compose cp api:/app/.mcp.json ./.mcp.json
```

This interactive command will:
- Create an admin user with login ID and password
- Set up MFA (TOTP) with your authenticator app (recommended)
- Create a personal workspace
- Generate an API key
- Write `.mcp.json` for MCP client configuration, then copy it to the repository root
- Configure the workspace's embedding provider when available

### 6. Access the Application

- **Web UI**: http://localhost:3000
- **Password sign-in**: Click "Sign in with password" on the login page and enter the admin login ID (or a verified email, see [Authentication](#authentication)) and password
- **API Docs**: http://localhost:8080/docs
- **Health Check**: http://localhost:8080/health

### 7. Save and recall your first memory

Create a context named `my-first-project` in the Web UI, then follow [MCP Client Setup](mcp-clients.md) for your client. Ask it to save a decision in that context and retrieve it in a new conversation; the [README walkthrough](../README.md#try-your-first-memory) gives copyable prompts.

Check that the memory appears in the UI and that the new conversation calls Kagura to retrieve it. Merely connecting a client does not import its past chats. If a tool needs `context_id`, use the UUID returned by `list_contexts`, not the context's display name.

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

For the Docker setup, run commands inside the API container from the repository root:

```bash
# Create admin (first time setup)
docker compose exec -e MCP_JSON_DIR=/app api python3 -m src.cli.create_admin

# Reset password or disable/re-enable MFA.
# A password reset also signs out every browser session, revokes the
# account's OAuth / MCP tokens, removes its passkeys and disconnects its
# Google / GitHub sign-ins, so Redis (REDIS_URL) must be reachable.
docker compose exec api python3 -m src.cli.reset_password

# Delete admin (for re-creation). When other users exist, a workspace the admin
# owns that they are in needs --transfer-to (one of its members) to take it
# over; without it the command lists them and stops. A workspace only the
# admin is in is deleted with it (#2371). Contexts the admin created pass to
# their workspace owner, except a private one: a linked contributor who wrote
# in it takes it over, and one nobody else wrote in is deleted (#2417).
# Ownership notifications follow commit.
# --workspace-transfer overrides --transfer-to for an individual workspace;
# repeat it when different workspaces have different successors.
# --yes skips the confirmation prompt.
docker compose exec api python3 -m src.cli.delete_admin [--transfer-to USER_ID_OR_EMAIL] [--workspace-transfer WORKSPACE_UUID=USER_ID_OR_EMAIL] [--yes]
```

For host-side development, activate `backend/.venv` and run the equivalent `python -m src.cli.…` from `backend/`. The Docker API container must be running; the host CLI reads `API_KEY_SECRET` from it.

## Authentication

Kagura Memory Cloud supports multiple authentication methods:

- **Password + MFA**: For the admin user (created via CLI), signing in with its login ID
- **Google OAuth**: Optional, configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env.local`
- **GitHub OAuth**: Optional, configure `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `.env.local`
- **Email + password**: An existing account can add a password from **Profile → Password** ("Set up a password" emails a one-time link to the account's address). Afterwards it can also sign in with its verified email and that password, and "Forgot password?" on the login page emails a reset link. This never creates an account — new accounts still come from Google / GitHub (and the signup gate) or the admin CLI.

The emailed links need a real email provider (`EMAIL_PROVIDER=resend`): the default `logging` provider records only that an email was due, never the link, so the links cannot be delivered by hand. See [Deployment → Email + password sign-in](deployment.md#email--password-sign-in-issue-1678).

## MCP Integration

The Web UI credentials page (`/workspace/integrations/credentials`) shows the MCP endpoint and the Claude Code OAuth one-liner in its connection card; the API Keys tab's **Connect with this key** section renders ready-to-paste snippets for each client below. The manual snippets in this section are the source of truth — keep them in sync if you change the supported transport shape.

### Which URL?

Every snippet below takes one of two endpoint URLs — same server, same API key:

- **Core tools (default):** `http://localhost:8080/mcp`
- **All tools:** `http://localhost:8080/mcp?profile=full`

An API key created on the credentials page is scoped to its workspace, so the URL needs no workspace segment. An OAuth client (Claude.ai, Claude Desktop, ChatGPT, Gemini, Cursor, `claude mcp add`) also uses `/mcp` and connects to the workspace selected in the web UI; `http://localhost:8080/mcp/w/YOUR_WORKSPACE_ID` pins such a connector (or a key minted before workspace scoping existed) to one workspace.

The default URL lists the core memory and context tools (the exact set is under Tool Profiles) and leaves out `explore`, Sleep, analyses, files, edges, secrets, resources and the agent control plane — they are just not listed — `describe_tools` names them from inside a session, and most clients let the model call listed tools only, so a task that needs them reconnects with the URL below. Add `?profile=full` when your client should see every tool schema up front (the list is about 2.7 times larger). See [Tool Profiles](mcp-tools.md#tool-profiles) for the exact tool set. The snippets show the default URL; in the Web UI, the **All tools** switch above them writes `?profile=full` into every snippet.

### Claude Code

The `create_admin` CLI automatically generates `.mcp.json`. If you need to create it manually:

```json
{
  "mcpServers": {
    "kagura-memory": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

All tools: set `"url"` to `"http://localhost:8080/mcp?profile=full"` instead ([which one?](#which-url)). The generated `.mcp.json` and `.mcp.json.example` both carry the default (core) URL.

Restart Claude Code to pick up the config, then test with `remember` and `recall` tools.

For Claude Desktop or Claude on the web, use the [remote connection instructions](mcp-clients.md#claude-desktop--claude-chat-web) instead of this project-level Claude Code file.

The **kagura-memory** Claude Code plugin adds session skills and tool-guardrail hooks that deliver `details.tool_trigger` memories at the matching tool call — setup and the `?guardrails=off` URL rule are under "Claude Code Plugin › Tool guardrails (hooks)" in [MCP Client Setup](mcp-clients.md).

### Cursor

Cursor reads the same `mcpServers` shape as Claude Code. Add the snippet above to your Cursor settings file (Settings → MCP), then restart Cursor.

### ChatGPT (Custom Connectors)

For hosted Kagura AI, install the [released Kagura Memory Cloud plugin](https://chatgpt.com/plugins/plugin_asdk_app_6adacc9eaf5481919a85fad2aaa7aade) and sign in to your Kagura account. The custom connection below is for your own Memory Cloud deployment.

Connect the server through ChatGPT's custom MCP flow, using your deployment's reachable HTTPS `/mcp` endpoint and Kagura sign-in. A raw `http://localhost:8080/mcp` URL is not reachable from ChatGPT's servers. OpenAI also documents Secure MCP Tunnel for private servers. Follow the [official connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) for the current interface and connection options, and [MCP Client Setup](mcp-clients.md#chatgpt-desktop) for Kagura-specific details.

For all tools, append `?profile=full` to the endpoint ([which one?](#which-url)).

ChatGPT runs no client hooks, so tool guardrails travel through the server: add `?guardrails=YOUR_CONTEXT_ID` to the URL (or `&guardrails=…` after `?profile=full`) and the connector's server instructions carry a digest of that context's guardrails — see [MCP Client Setup › ChatGPT web (developer mode)](mcp-clients.md#chatgpt-web-developer-mode).

### Codex CLI

Checked against Codex `rust-v0.155.1`. Codex reads the API key from an environment variable that its config names — the key itself never goes into `~/.codex/config.toml`.

**Recommended — `codex mcp add`:**

```bash
export KAGURA_API_KEY="kagura_xxxxxxxxxxxx"
codex mcp add kagura-memory --url "http://localhost:8080/mcp" --bearer-token-env-var KAGURA_API_KEY
```

`codex mcp list` now shows `kagura-memory` with `KAGURA_API_KEY` as its bearer token env var. Export `KAGURA_API_KEY` in every shell you start Codex from (or in your shell profile).

**Manual equivalent — `~/.codex/config.toml`:**

The command writes this entry; paste it yourself if you prefer editing the file:

```toml
[mcp_servers.kagura-memory]
url = "http://localhost:8080/mcp"
bearer_token_env_var = "KAGURA_API_KEY"
```

These two keys only. Codex rejects an inline `bearer_token` on an HTTP server, and the whole `config.toml` then fails to load ([Troubleshooting](troubleshooting.md#codex-cli--bearer_token-is-not-supported-for-streamable_http)); `type` is not a Codex key.

All tools: set `url = "http://localhost:8080/mcp?profile=full"` instead ([which one?](#which-url)).

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
2. Add `?guardrails=off` to the `url` above (`&guardrails=off` when the URL already has a query, such as `?profile=full`) so the server does not also send a guardrail digest.
3. Open `/hooks` in Codex and trust the kagura-memory hooks. Codex skips plugin-bundled hooks until each user trusts them, and asks again only when `hooks.json` changes.

The hooks read the `[mcp_servers.kagura-memory]` entry above (`bearer_token_env_var`, `env_http_headers` or `http_headers` — exactly one; never a project-level `.codex/config.toml`) and need `python3` 3.11+ on `PATH`. Windows, web and cloud tasks are not supported. If nothing happens, see [Troubleshooting](troubleshooting.md#codex-cli--kagura-memory-hooks-never-run).

## Quick API Test

Create a context in the Web UI first. Replace `YOUR_CONTEXT_UUID` below with its UUID (also returned by the MCP `list_contexts` tool). REST uses `context.context_id` on writes and `filters.context_id` on searches; this differs from the MCP tools' top-level `context_id`.

Run in Bash with `curl`. Enter an API key from the credentials page at the hidden prompt:

```bash
read -rs -p 'Kagura API key: ' KAGURA_API_KEY
export KAGURA_API_KEY

# Remember a memory
curl -X POST http://localhost:8080/api/v1/memory/remember \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "PostgreSQL chosen for transactions and relational queries",
    "content": "The project uses PostgreSQL because it needs transactions and relational queries.",
    "context": {"context_id": "YOUR_CONTEXT_UUID"},
    "type": "decision",
    "importance": 0.8
  }'

# Recall memories
curl -X POST http://localhost:8080/api/v1/memory/recall \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Why did we choose PostgreSQL?",
    "filters": {"context_id": "YOUR_CONTEXT_UUID"},
    "k": 5
  }'
```

## Next Steps

- [API Reference](api-reference.md) — Detailed API documentation
- [Benchmarks](benchmarks.md) — Answer accuracy, cost, and retrieval evaluations
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
docker compose exec api bash -c 'cd /app && PYTHONPATH=src alembic upgrade head'
```

### MFA locked out

```bash
docker compose exec api python3 -m src.cli.reset_password
# Choose option 2 (Disable MFA) or 3 (Both)
```

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **API Docs**: http://localhost:8080/docs
