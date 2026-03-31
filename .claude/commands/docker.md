---
description: Docker operations (rebuild, restart, logs, status, reset)
---

Manage Docker services for Kagura Memory Cloud.

## Usage

`/docker <action> [service...]`

**Actions:**
- `rebuild` — Rebuild and restart services (default: api web)
- `restart` — Restart services without rebuilding
- `logs` — Show recent logs (default: api, last 50 lines)
- `status` — Show service status and health
- `reset` — Stop all, remove volumes, rebuild from scratch (asks confirmation)
- `update` — Pull latest source changes into containers (rebuild + restart)

**Services:** api, web, postgres, qdrant, redis (or `all`)

## Steps

### 1. Parse arguments

Extract `<action>` and optional `[service...]` from: $ARGUMENTS

Default action: `status`
Default services: `api web` (for rebuild/restart/update), `api` (for logs)

### 2. Execute action

#### status
```bash
docker compose ps
docker compose ps --format json | python3 -c "
import json, sys
for line in sys.stdin:
    s = json.loads(line)
    print(f\"  {s['Service']}: {s['State']} ({s.get('Health', 'N/A')})\")
"
```

#### rebuild
```bash
docker compose build --no-cache <services>
docker compose up -d <services>
docker compose ps
```

#### restart
```bash
docker compose restart <services>
docker compose ps
```

#### logs
```bash
docker compose logs --tail=50 <services>
```
If user requests more: `docker compose logs --tail=200 <services>`

#### update
Same as rebuild — rebuild images with latest source and restart.

#### reset
**Ask for user confirmation first!** This destroys all data.
```bash
docker compose down -v
docker compose build --no-cache
docker compose up -d
```
Then remind user to run `/setup` steps 4-5 (migrations + admin account).

### 3. Report result

Show service status after any mutating action.
If any service is unhealthy, show its logs (`docker compose logs --tail=20 <service>`).
