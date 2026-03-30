---
description: Set up Kagura Memory Cloud from scratch (Docker services, DB migration, admin account)
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

**Minimum:** 2 cores, 4 GB RAM, 10 GB disk
**Recommended:** 4+ cores, 8+ GB RAM, 20+ GB disk

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
If missing:
```bash
cp .env.example .env.local
```
OAuth providers (Google, GitHub) are optional — admin login uses password + MFA.

### 3. Start Docker services
```bash
docker compose up -d
```
Wait for all services to be healthy:
```bash
docker compose ps
```
Expected: postgres, qdrant, redis, api, web — all healthy/running.

### 4. Run database migrations
```bash
cd backend && alembic upgrade head
```

### 5. Create admin account

Prompt the user to run this command interactively (it requires keyboard input for password and MFA):

```
! cd backend && API_KEY_SECRET="$(grep API_KEY_SECRET ../docker-compose.yml | head -1 | sed 's/.*: *//' | tr -d '"')" python -m src.cli.create_admin
```

This will:
- Create admin user with login ID + password
- Set up MFA (TOTP) — recommended, requires authenticator app
- Create personal workspace
- Generate API key
- Write `.mcp.json` for MCP client configuration

### 6. Verify

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
curl -s http://localhost:8080/api/v1/auth/config | python3 -m json.tool
```
Expected: `{"status": "ok"}` and auth config showing enabled methods.

### 7. Test MCP connection

After `.mcp.json` is written, tell the user to restart Claude Code, then test:
- `remember` — store a test memory
- `recall` — search for it

### 8. Admin CLI reference

Inform the user of available admin commands (all from `backend/` directory):

| Command | Purpose |
|---------|---------|
| `python -m src.cli.create_admin` | Create admin + workspace + API key + .mcp.json |
| `python -m src.cli.reset_password` | Reset password and/or MFA |
| `python -m src.cli.delete_admin` | Delete admin (for re-creation) |

> Set `API_KEY_SECRET` env var when running CLI commands that involve MFA or API keys.

Report the status of each step. If any step fails, diagnose and suggest fixes.
