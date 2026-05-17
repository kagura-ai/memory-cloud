---
paths:
  - "backend/**"
---

# Backend Rules (FastAPI / Python)

## Imports
- Database: `from db.base import Base, get_db` (async SQLAlchemy)
- Auth: `from auth.dependencies import get_current_user, APIKeyOrSessionUser` (APIKeyOrSessionUser is an `Annotated` type alias)
- Logger: `from utils.logger import get_logger`
- Never import `declarative_base()` directly

## Patterns
- All route handlers must be `async def`
- Use `Depends(get_db)` for database access
- Use `Depends(get_current_user)` or `Depends(APIKeyOrSessionUser)` for auth
- Type hints required on all function signatures
- Google-style docstrings on public functions
- Use `structlog` logger, never `print()`

## Database
- Use async SQLAlchemy (`AsyncSession`) for all DB operations
- Synchronous engine only for OAuth2 server (Authlib requirement)
- Pool config: `pool_size=5, max_overflow=10, pool_pre_ping=True`

## Datetime / UTC

**Storage (DB)** — columns are `TIMESTAMP WITHOUT TIME ZONE`, holding naive UTC. UTC is enforced explicitly at three layers, so a naive value here is unambiguous:
1. Postgres container env: `TZ=UTC` + `PGTZ=UTC` (`docker-compose.yml`, `terraform/single-server/docker-compose.prod.yml`)
2. SQLAlchemy engine: async `connect_args={"server_settings": {"timezone": "UTC"}}` / sync `connect_args={"options": "-c timezone=utc"}` (`db/base.py`)
3. Python writes via `utils.datetime.utcnow()` — never bare `datetime.utcnow()` or `datetime.now()` without `tz`

`ruff DTZ` blocks naive-datetime patterns in `backend/src/`. Tests are exempt via `[tool.ruff.lint.per-file-ignores]` while the fixture sweep remains a follow-up.

**Wire format (API JSON)** — response Pydantic schemas with any `datetime` field MUST inherit from `models.api_base.TZAwareBaseModel` (not `BaseModel`). The base class serializes naive datetimes with a `Z` suffix; without it, JS clients parse the string as local time and JST users see times shifted by their UTC offset.

For `str`-typed datetime fields in handler/service code (manual ISO formatting), use `to_utc_iso(dt)` from `utils.datetime`. Never call `.isoformat()` directly on a `datetime` field that ends up in a JSON response or cached payload — `to_utc_iso` is idempotent (handles None / naive / aware UTC / aware non-UTC) so it can be applied unconditionally.

**Column-type migration was evaluated in #490 and explicitly deferred.** The full `Column(DateTime, ...) → Column(DateTime(timezone=True), ...)` sweep was scoped at 113 columns + 188 caller sites and judged hygiene-only after #489 closed the user-visible bug. The three layers above provide equivalent correctness guarantees; do not "fix" the naive columns without re-litigating the trade-off.

## Testing
- Test files: `backend/tests/{module}/test_{name}.py`
- Use `pytest-asyncio` fixtures from `conftest.py`
- Mock auth: `app.dependency_overrides[get_current_user]`

## Forbidden
- No f-string SQL queries (use SQLAlchemy ORM or `text()` with bind params)
- No synchronous DB calls in async context
- No `print()` statements
- No hardcoded secrets or API keys
