# Contributing to Kagura Memory Cloud

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

### Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Node.js 20+
- Git

### Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud

# 2. Copy environment config
cp .env.example .env.local

# 3. Start services
docker compose up -d

# 4. Run database migrations
cd backend && alembic upgrade head

# 5. Verify
curl http://localhost:8080/health  # Backend
open http://localhost:3000          # Frontend
```

### Manual Setup (without Docker)

See the [README](README.md) for platform-specific instructions (WSL, macOS, Linux).

## Code Style

### Backend (Python)

- **Naming**: `snake_case` (functions/variables), `PascalCase` (classes), `UPPER_SNAKE_CASE` (constants)
- **Type hints**: Required on all function signatures
- **Docstrings**: Google style on public functions
- **Logger**: Use `structlog` via `from utils.logger import get_logger` — never `print()`
- **Database**: Always use async SQLAlchemy (`AsyncSession`) — synchronous only for OAuth2 (Authlib)
- **SQL**: Never use f-strings for SQL queries — use SQLAlchemy ORM or `text()` with bind parameters

```bash
# Lint
cd backend && ruff check src/ tests/

# Format
cd backend && ruff format src/ tests/

# Type check
cd backend && pyright src/
```

### Frontend (TypeScript)

- **Components**: `PascalCase` (`MemoryCard.tsx`)
- **Utilities**: `camelCase` (`formatDate.ts`)
- **Types**: `PascalCase` with descriptive names
- **Forbidden**: No `any` type, no `console.log`, no hardcoded API URLs

```bash
cd frontend && npm run build
```

## Testing

### Running Tests

```bash
# All tests
cd backend && pytest -v --maxfail=5

# Smoke tests (fast, no DB required)
make test-smoke

# E2E tests
make test-e2e

# URL validation (all routes)
make test-urls

# Integration tests (DB, migrations, attachments)
make test-integration

# Frontend tests
make test-frontend

# With coverage
cd backend && pytest --cov=src --cov-report=html
```

### Writing Tests

- Place test files in `backend/tests/{module}/test_{name}.py`
- Use `pytest-asyncio` for async tests
- Mock auth with `app.dependency_overrides[get_current_user]`
- Target coverage: 90%+

## Continuous Integration

CI runs via `.github/workflows/ci.yml` on pull requests against `main`, on tag pushes (`v*`), and via manual `workflow_dispatch`.

### Jobs

| Job | When it runs | Services |
|---|---|---|
| `lint` | All triggers | none |
| `backend-unit` | All triggers | redis |
| `frontend` | All triggers | none |
| `detect-changes` | All triggers | none |
| `backend-integration` | `detect-changes` match **or** `force-integration` label **or** `workflow_dispatch` **or** tag push | postgres + redis |

`backend-integration` declares `needs: [detect-changes, backend-unit]`, so unit-failing PRs never pay the integration-suite cost. See `.github/workflows/ci.yml` for exact commands.

### Triggering integration tests on a PR

Integration tests run automatically when the PR changes paths matching the `detect-changes` filter (`backend/src/**`, `backend/alembic/versions/**`, `backend/tests/integration/**`, `backend/tests/smoke/test_all_routes.py`, `backend/pyproject.toml`, or `.github/workflows/ci.yml`).

To force-run integration tests on a PR that doesn't touch those paths, add the `force-integration` label.

For local pre-PR verification, `make test-integration` runs the same suite against your Docker services and is the canonical pre-PR check for ORM/migration changes.

### Manual run

Use the GitHub **Actions** tab → **CI** → **Run workflow** (the `workflow_dispatch` trigger). `backend-integration` runs unconditionally in this mode.

## Branch Strategy

```
main (default)
├── {issue-number}-feat/{description}   # New features
├── {issue-number}-fix/{description}    # Bug fixes
├── {issue-number}-refactor/{description}
├── {issue-number}-test/{description}
└── {issue-number}-docs/{description}
```

- Branch from `main`, merge back to `main` (squash merge)
- Branch lifespan: max 7 days
- Keep up to date: `git rebase origin/main`
- Releases: tag-based (`v1.0.0`, `v1.1.0`, ...)

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject> (#issue-number)

[optional body]
```

**Types**: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

**Scopes**: `api`, `mcp`, `auth`, `db`, `infra`, `frontend`, `docs`

**Examples**:
```
feat(mcp): add create_context tool (#309)
fix(api): apply scope filter to memory list count (#296)
test: add smoke/E2E/URL validation tests (#300)
```

## Pull Requests

1. Create a branch from `main`
2. Make your changes
3. Run quality checks: `make lint && make test-local`
4. Push and create a PR against `main`
5. Fill in the PR template

**PR title**: Keep under 70 characters, use Conventional Commits format.

## Database Migrations

Migrations are managed with Alembic:

```bash
# Create a new migration
make migrate-create name="add_new_column"

# Run pending migrations
make migrate

# Check current status
make migrate-status

# Rollback last migration
make migrate-down
```

**Important**: Review auto-generated migrations before applying — remove any legacy schema differences.

## Development with Claude Code

This project includes pre-configured Claude Code tooling in `.claude/`. See the [README — Getting Started with Claude Code](README.md#getting-started-with-claude-code) for setup instructions.

- **Slash commands**: `/test`, `/quality`, `/self-review`, `/self-maint`, `/issue-start`, `/remember`, `/recall`
- **Hooks**: Auto-format (ruff/prettier), secret detection, SQL injection prevention, memory sync
- **Agents**: `code-reviewer`, `test-runner`
- **Rules**: Backend, frontend, and security coding standards (auto-loaded by file path)

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
