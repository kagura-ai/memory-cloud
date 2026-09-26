# Kagura Memory Cloud Backend

FastAPI + MCP Server for Universal AI Memory Platform

## Requirements

- Python 3.11+
- uv 0.11.19 — the exact version `[tool.uv] required-version` in `pyproject.toml` names (`curl -LsSf https://astral.sh/uv/0.11.19/install.sh | sh`, or `pip install "uv==0.11.19"` inside a venv); a different uv refuses to run here

## Installation

Dependencies are installed from the tracked lock, `uv.lock`, so every checkout,
CI job and the Docker image run the same releases:

```bash
# Dev dependencies (tests, ruff, pyright) — creates ./.venv
uv sync --locked --extra dev

# With neural memory support
uv sync --locked --extra dev --extra neural

# Then either activate the environment or prefix commands with `uv run`
source .venv/bin/activate
uv run pytest
```

### Updating dependencies

`pyproject.toml` holds the version ranges (what the code supports); `uv.lock`
holds what is tested and shipped. After editing `pyproject.toml`, regenerate
the lock and commit both — CI's `uv lock --check` fails otherwise:

```bash
uv lock                          # re-resolve after a pyproject.toml change
uv lock --upgrade-package NAME   # move one package inside its range
```

Renovate opens a weekly lock-maintenance PR that refreshes every package inside
its range; it runs the full CI like any other change.

## Development

### Run locally

```bash
# Development mode with hot reload
uvicorn src.main:app --reload --port 8080

# Or use the Python module directly
python -m src.main
```

### Testing

```bash
# Run tests
pytest

# With coverage
pytest --cov

# With coverage report
pytest --cov --cov-report=html
open htmlcov/index.html
```

### Linting & Type Checking

```bash
# Ruff (linting & formatting)
ruff check src/
ruff format src/

# Pyright (type checking)
pyright src/
```

## Docker

### Build

```bash
docker build -t kagura-backend:latest .
```

### Run

```bash
docker run -p 8080:8080 \
  -e DATABASE_URL=postgresql://... \
  -e QDRANT_URL=http://... \
  -e REDIS_URL=redis://... \
  kagura-backend:latest
```

## API Documentation

Once the server is running, visit:

- Swagger UI: http://localhost:8080/docs
- ReDoc: http://localhost:8080/redoc

## Project Structure

```
backend/
├── src/
│   ├── api/            # REST API endpoints
│   ├── auth/           # OAuth2 + JWT authentication
│   ├── core/
│   │   ├── memory/     # 3-layer memory system
│   │   ├── search/     # Hybrid search (Semantic + BM25)
│   │   ├── embedding/  # OpenAI embeddings
│   │   ├── neural/     # Neural Memory (Hebbian learning)
│   │   └── graph/      # Graph memory (NetworkX)
│   ├── db/             # Database models (PostgreSQL + Qdrant)
│   ├── config/         # Configuration
│   └── main.py         # Application entry point
├── tests/
├── pyproject.toml
├── Dockerfile
└── README.md
```

## Technology Stack

- **Web Framework**: FastAPI 0.133+
- **ASGI Server**: Uvicorn
- **Database**: PostgreSQL 18+ (SQLAlchemy 2.1 + asyncpg; CI/local/production all run the digest-pinned 18.4 — the 15→18 migration record lives in `docs/ops/postgres-18-migration-runbook.md`)
- **Vector DB**: Qdrant 1.15+
- **Cache**: Redis 7+
- **Graph Memory**: NetworkX 3.0+
- **Authentication**: OAuth2 and JWT (Authlib 1.8)
- **LLM APIs**: OpenAI (embeddings), Cohere (reranking)
- **Testing**: pytest, pytest-asyncio, pytest-cov
- **Type Checking**: Pyright
- **Linting**: Ruff

## Environment Variables

See `.env.example` in the root directory.

Required:
- `DATABASE_URL` - PostgreSQL connection string
- `QDRANT_URL` - Qdrant server URL
- `REDIS_URL` - Redis connection string
- `GOOGLE_CLIENT_ID` - OAuth2 client ID
- `GOOGLE_CLIENT_SECRET` - OAuth2 client secret

## License

Apache License 2.0 - See [LICENSE](../LICENSE)
