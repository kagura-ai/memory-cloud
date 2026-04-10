---
paths:
  - "docker-compose*"
  - "Makefile"
  - "backend/tests/**"
  - ".env*"
---

# Development Environment

## Docker Compose

- **Only `docker-compose.yml` exists** — there is no `docker-compose.dev.yml` or `docker-compose.override.yml`
- All environments (dev, test, prod) use the same compose file with environment variables for differentiation
- Container names: `kagura-api`, `kagura-web`, `kagura-db`, `kagura-redis`

## Test Execution

| Command | What it runs | Requires Docker? |
|---------|-------------|-----------------|
| `make test-local` | Unit tests (excludes e2e/integration) | No |
| `make test-frontend` | Vitest frontend tests | No |
| `make test-integration` | DB + migration tests | Yes (DB container) |
| `make test-e2e` | End-to-end tests | Yes |
| `make test-smoke` | Health, auth, well-known endpoints | Yes |
| `make test` | All tests via Docker exec | Yes |
| `make test-unit` | Unit tests (api/neural/utils/auth) | No |

For local backend tests: `cd backend && pytest tests/api/ -v`

## Python Environment

- Backend source: `backend/src/`
- `PYTHONPATH=src` is required when running scripts outside pytest (pytest configures this via `pyproject.toml`)
- Virtual env: `backend/.venv/` (not committed)
- Dependencies: `backend/requirements.txt`

## Key Makefile Targets

| Target | Purpose |
|--------|---------|
| `make up` / `make down` | Start/stop all Docker services |
| `make restart` | Restart API container only |
| `make rebuild` | No-cache rebuild + restart |
| `make logs` | Follow API logs |
| `make lint` | ruff check (backend) |
| `make format` | ruff format (backend) |
| `make type-check` | pyright (backend) |
| `make migrate` | Run pending Alembic migrations |
| `make migrate-create name='...'` | Create new migration |
| `make health` | Curl API health endpoint |

## Things That Do NOT Exist

- `docker-compose.dev.yml` — does not exist, never existed
- `memory-dev` VM — no separate dev VM
- `backend/manage.py` — this is not Django
- `npm start` in frontend — Next.js dev server runs inside Docker via `kagura-web` container
