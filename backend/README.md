# Kagura Memory Cloud Backend

FastAPI + MCP Server for Universal AI Memory Platform

## Requirements

- Python 3.11+
- uv (recommended) or pip

## Installation

### Using uv (recommended)

```bash
# Install basic dependencies
uv pip install -e .

# Install with dev dependencies
uv pip install -e ".[dev]"

# Install with neural memory support
uv pip install -e ".[dev,neural]"
```

### Using pip

```bash
# Install basic dependencies
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Install with neural memory support
pip install -e ".[dev,neural]"
```

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

- **Web Framework**: FastAPI 0.115+
- **ASGI Server**: Uvicorn
- **Database**: PostgreSQL 18+ (SQLAlchemy + asyncpg; CI/local/production all run the digest-pinned 18.4 — the 15→18 migration record lives in `docs/ops/postgres-18-migration-runbook.md`)
- **Vector DB**: Qdrant 1.15+
- **Cache**: Redis 7+
- **Graph Memory**: NetworkX 3.0+
- **Authentication**: OAuth2 (Authlib) + JWT (python-jose)
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
