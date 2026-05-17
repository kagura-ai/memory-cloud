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

**Storage (DB) — mixed-awareness, predominantly naive UTC.** Most columns are `TIMESTAMP WITHOUT TIME ZONE` (naive UTC by convention); 11 columns across 5 models are `TIMESTAMP WITH TIME ZONE` (aware): `Context.last_used_at` (auth.py), and timestamps in `bm25_drift.py`, `erasure.py`, `hub_tag.py`, and `neural.py`. **Always check the model's `mapped_column(DateTime[, timezone=True])` declaration before constructing comparison sentinels or default values**, never assume naive across the board.

UTC is enforced for every SQLAlchemy session by the engine, regardless of postgres server defaults:

1. **SQLAlchemy engine `connect_args` (primary guarantee for application code)** — async: `connect_args={"server_settings": {"timezone": "UTC"}}`; sync: `connect_args={"options": "-c timezone=utc"}` (`db/base.py`). Every connection runs `SET timezone='UTC'` at handshake, so any `now()` / `current_timestamp` inside the session is UTC, and naive DB values written via `utcnow()` round-trip unambiguously.
2. **Container env `TZ=UTC` + `PGTZ=UTC`** (`docker-compose.yml`, `terraform/single-server/docker-compose.prod.yml`) — these align the *container OS clock* and *libpq client* (e.g. `psql`, `pg_isready`) with UTC, so cron / log timestamps / interactive sessions don't drift. They do **not** rewrite the postgres server's `timezone` GUC in an already-initialized `postgres_data` volume — that's set at `initdb` time. The engine layer above is what guarantees application correctness.
3. **Python writes** — call `utils.datetime.utcnow()`, never bare `datetime.utcnow()` or `datetime.now()` without `tz`. `ruff DTZ` blocks the bare forms in `backend/src/`; tests are exempt via `[tool.ruff.lint.per-file-ignores]` while the fixture sweep remains a follow-up.

When constructing a sort-key or default for a column declared `DateTime(timezone=True)`, use an **aware** sentinel (e.g. `datetime.min.replace(tzinfo=UTC)`); naive `datetime.min` will `TypeError` on comparison with aware values when the list contains both `None` and populated rows.

**Wire format (API JSON)** — response Pydantic schemas with any `datetime` field MUST inherit from `models.api_base.TZAwareBaseModel` (not `BaseModel`). The base class serializes naive datetimes with a `Z` suffix; without it, JS clients parse the string as local time and JST users see times shifted by their UTC offset.

For `str`-typed datetime fields in handler/service code (manual ISO formatting), use `to_utc_iso(dt)` from `utils.datetime`. Never call `.isoformat()` directly on a `datetime` field that ends up in a JSON response or cached payload — `to_utc_iso` is idempotent (handles None / naive / aware UTC / aware non-UTC) so it can be applied unconditionally.

**Column-type migration was evaluated in #490 and explicitly deferred.** The full `Column(DateTime, ...) → Column(DateTime(timezone=True), ...)` sweep was scoped at ~100 naive columns + caller migration and judged hygiene-only after #489 closed the user-visible bug. The engine + container + Python layers above provide equivalent correctness guarantees for the naive-column path; do not "fix" the naive columns without re-litigating the trade-off.

## Testing
- Test files: `backend/tests/{module}/test_{name}.py`
- Use `pytest-asyncio` fixtures from `conftest.py`
- Mock auth: `app.dependency_overrides[get_current_user]`

## Forbidden
- No f-string SQL queries (use SQLAlchemy ORM or `text()` with bind params)
- No synchronous DB calls in async context
- No `print()` statements
- No hardcoded secrets or API keys
