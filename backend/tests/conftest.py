"""Pytest configuration and fixtures for Kagura Memory Cloud tests."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from models.auth import Base as AuthBase
from models.memory import Base as MemoryBase

# Issue #471: import the cost-grade pricing model so its table is
# registered with the shared declarative ``Base.metadata`` and gets
# created by ``create_all`` below. Sleep models are picked up
# transitively via service imports today; importing explicitly here
# keeps the test setup robust to future import-graph changes.
import models.llm_pricing  # noqa: F401  isort: skip
import models.llm_call_log  # noqa: F401  isort: skip  # Issue #474: comprehensive LLM call ledger
import models.sleep  # noqa: F401  isort: skip
import models.analysis  # noqa: F401  isort: skip  # Issue #494: Memory Broadlistening tables

# Configure structlog the same way api/main.py does at app startup so
# logger.info("event", key=value) calls inside route modules work in
# pytest too. Without this, structlog's BoundLoggerLazyProxy falls back
# to wrapping stdlib logging.Logger, which rejects kwargs with
# ``TypeError: Logger._log() got an unexpected keyword argument 'X'``
# the first time any structured-kwargs logger.info/.warning is hit.
# Production runs setup_logger() from api/main.py:25; pytest skips that
# entry point and imports route modules directly, so we mirror it here
# so the project's structlog kwargs convention works uniformly across
# both contexts (Copilot review on PR #522).
from utils.logger import setup_logger as _setup_logger  # noqa: E402

_setup_logger()


@pytest.fixture(autouse=True)
def _clear_pricing_cache():
    """Reset the process-local ``llm_pricing`` cache around every test (#713).

    ``LLMPricingService`` caches resolved prices in a module-global ``TTLCache``
    for the recall hot path. Without clearing it between tests, a pricing row
    seeded by one test would leak into another that seeds a different price for
    the same ``(provider, model, unit_type, date)`` key — making cost
    assertions order-dependent. Cheap (a dict clear) so applied unconditionally.
    """
    from services.llm_pricing_service import clear_pricing_cache

    clear_pricing_cache()
    yield
    clear_pricing_cache()


def pytest_configure(config: pytest.Config) -> None:
    """Validate asyncio_default_test_loop_scope matches fixture loop scope.

    Session-scoped fixtures (async_engine, db_session) require session-scoped loops.
    Configure via pyproject.toml: asyncio_default_test_loop_scope = "session"
    """
    try:
        scope = config.getini("asyncio_default_test_loop_scope")
    except ValueError:
        return
    if scope != "session":
        raise pytest.UsageError(
            "asyncio_default_test_loop_scope must be 'session' in pyproject.toml "
            "to match session-scoped async fixtures (async_engine, db_session)."
        )


_E2E_DIR = Path(__file__).parent / "e2e"


def _e2e_explicitly_requested(config: pytest.Config) -> bool:
    """True only when the caller deliberately opted into the e2e suite.

    Opt-in signals: a CLI path under ``tests/e2e`` (how ``make test-e2e``
    invokes pytest), an ``-m e2e`` marker selection, or ``RUN_E2E=1``.
    """
    if os.environ.get("RUN_E2E"):
        return True
    markexpr = config.getoption("markexpr", default="") or ""
    if "e2e" in markexpr:
        return True
    return any("e2e" in str(arg) for arg in config.invocation_params.args)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Keep the Playwright e2e suite out of in-process unit/integration runs.

    ``tests/e2e`` drives Playwright's *sync* API (``sync_playwright()`` in
    ``tests/e2e/conftest.py``), which runs its own asyncio event loop on the
    main thread. The rest of the suite shares a single session-scoped loop
    (``asyncio_default_test_loop_scope = "session"``). Mixing the two in one
    process leaves Playwright's loop current, so every ``pytest-asyncio`` test
    collected after ``tests/e2e`` dies with
    ``RuntimeError: Runner.run() cannot be called from a running event loop``.

    The Makefile targets already pass ``--ignore=tests/e2e``, but a bare
    ``pytest`` / ``pytest tests/`` (``testpaths = ["tests"]``) would otherwise
    pull e2e in and produce ~1000 misleading failures. Skip the e2e subtree
    unless it is explicitly requested (see ``_e2e_explicitly_requested``).
    """
    if collection_path != _E2E_DIR and _E2E_DIR not in collection_path.parents:
        return None  # not an e2e path — defer to default collection
    if _e2e_explicitly_requested(config):
        return None
    return True


# Test database URL — default to localhost (Docker port-mapped)
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine():
    """Create async engine for tests. Skip if DB is unavailable."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        echo=False,
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
            await conn.run_sync(MemoryBase.metadata.create_all)
    except Exception as e:
        await engine.dispose()
        pytest.skip(f"Test database not available: {e}")

    yield engine

    # Cleanup
    try:
        async with engine.begin() as conn:
            await conn.run_sync(MemoryBase.metadata.drop_all)
            await conn.run_sync(AuthBase.metadata.drop_all)
    except Exception:
        pass  # Ignore cleanup errors
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create database session for tests."""
    async_session_maker = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session_maker() as session:
        try:
            yield session
        finally:
            try:
                await session.rollback()
            except Exception:
                pass  # Ignore rollback errors
            try:
                await session.close()
            except Exception:
                pass  # Ignore close errors


# Non-async fixtures for simple data
@pytest_asyncio.fixture
def test_user_id() -> str:
    """Test user ID."""
    return "test_user_123"


@pytest_asyncio.fixture
def test_memory_data() -> dict:
    """Test memory data."""
    return {
        "summary": "テストメモリー：認証エラー修正",
        "context_summary": "ユーザーからログイン失敗の報告があり、調査を開始。",
        "content": "auth.pyのverify_token関数にexpired_atの検証を追加",
        "details": {"code_diff": "...", "test_results": "All tests passed"},
        "type": "code",
        "importance": 0.8,
        "confidence": 1.0,
        "tags": ["python", "authentication"],
        "context": {"context_id": "test-context", "file_path": "auth.py"},
    }
