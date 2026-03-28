---
description: Set up Kagura Memory Cloud from scratch (Docker services, DB migration, frontend)
---

Set up the local development environment from scratch.

## Steps

### 0. Detect platform and check prerequisites

```bash
uname -s && uname -r
```

Determine the platform from the output and guide accordingly:

**WSL (Windows)** — `uname -r` contains `microsoft` or `WSL`:
- Docker: Requires [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/) installed on the **Windows side**
  - Enable "Use the WSL 2 based engine" in Settings
  - Enable WSL integration for the distro in Settings → Resources → WSL Integration
  - After install: `wsl --shutdown` from PowerShell, then reopen terminal
- Python: `sudo apt install python3.11 python3.11-venv python3-pip`
- Node.js: `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs`

**macOS** — `uname -s` is `Darwin`:
- Docker: Install [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)
- Python: `brew install python@3.11`
- Node.js: `brew install node@20`

**Linux (Ubuntu/Debian)** — `uname -s` is `Linux` without `microsoft` in `uname -r`:
- Docker: `sudo apt install docker.io docker-compose-v2 && sudo usermod -aG docker $USER` (re-login after)
- Python: `sudo apt install python3.11 python3.11-venv python3-pip`
- Node.js: `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs`

Do NOT proceed until all prerequisites are available.

### 0.5. Check system resources
```bash
nproc && free -h | head -2 && df -h / | tail -1
```

**Minimum requirements:**
- CPU: 2 cores
- RAM: 4 GB
- Disk: 10 GB free

**Recommended:**
- CPU: 4+ cores
- RAM: 8+ GB
- Disk: 20+ GB free

If below minimum, warn the user that services may be unstable.

### 1. Verify prerequisites
```bash
docker --version && docker compose version
python3 --version
node --version
```

If any command fails, guide the user with the platform-specific install instructions from Step 0.

### 2. Check .env.local exists
```bash
ls -la .env.local
```
If missing, copy from example:
```bash
cp .env.example .env.local
```
Remind the user to set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `OPENAI_API_KEY` in `.env.local`.

### 3. Start Docker services
```bash
docker compose up -d
```
Wait for all services to be healthy:
```bash
docker compose ps
```
Expected: postgres, qdrant, redis, api — all healthy/running.

### 4. Run database migrations
```bash
cd backend && alembic upgrade head
```

### 5. Install frontend dependencies and start dev server
```bash
cd frontend && npm install && npm run dev
```

### 6. Verify
```bash
curl -s http://localhost:8080/health | jq .
```
Expected: `{"status": "ok"}`

### 7. MCP setup guide

After all services are running, guide the user through MCP client setup:

1. **Create an API key**: Open http://localhost:3000/workspace/integrations/api-keys in a browser and create a new API key. Copy the key (starts with `kagura_`).

2. **Find your workspace ID**: The workspace ID is in the URL bar when logged in (e.g., `http://localhost:3000/workspace/...`). Or query it from the API key table:
```bash
docker exec kagura-postgres psql -U kagura -d kagura -c "SELECT workspace_id FROM api_keys ORDER BY created_at DESC LIMIT 1;"
```

3. **Create `.mcp.json`** in the project root (or any repo where you want to use Kagura Memory):
```json
{
  "mcpServers": {
    "kagura-memory": {
      "type": "http",
      "url": "http://localhost:8080/mcp/w/{WORKSPACE_ID}",
      "headers": {
        "Authorization": "Bearer {YOUR_API_KEY}"
      }
    }
  }
}
```
Replace `{WORKSPACE_ID}` and `{YOUR_API_KEY}` with actual values.

4. **Restart Claude Code** to pick up the new MCP config, then verify with:
   - `remember` — store a test memory
   - `recall` — search for it

Report the status of each step. If any step fails, diagnose and suggest fixes.
