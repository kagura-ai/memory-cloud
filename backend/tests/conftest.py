"""Pytest configuration and fixtures for Kagura Memory Cloud tests."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import NoReturn

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
def _capacity_gate_needs_a_session(monkeypatch):
    """Run ``MemoryService._ensure_capacity`` only against a session (#1941).

    The capacity-lock check reads the workspace and two counts at the entry of
    every memory read/write. Hundreds of service tests drive those methods
    with a bare ``MagicMock`` session whose ``execute`` answers a scripted
    sequence; the extra reads would consume it. The check therefore runs only
    when ``self.db`` is an ``AsyncSession`` — a real one (integration tests)
    or a ``MagicMock(spec=AsyncSession)``, which is how
    ``tests/services/test_memory_service_capacity_lock.py`` exercises it.
    """
    from services.memory_service import MemoryService

    real = MemoryService._ensure_capacity

    async def _gated(self, *args, **kwargs):
        if isinstance(self.db, AsyncSession):
            await real(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "_ensure_capacity", _gated)


# Route modules that call the capacity lock directly (#1941), and the name each
# binds. Their tests drive the handlers with a mocked ``db`` / ``perm`` /
# service whose session is a bare MagicMock; the same rule as above applies.
_CAPACITY_GATED_ROUTES = (
    ("api.routes.agent_state", "ensure_context_not_capacity_locked"),
    ("api.routes.feedback", "ensure_context_not_capacity_locked"),
    ("api.routes.share_keys", "ensure_context_not_capacity_locked"),
    ("api.routes.graph", "ensure_not_capacity_locked"),
    ("api.routes.analyses", "ensure_not_capacity_locked"),
    ("api.routes.sleep_reports", "ensure_not_capacity_locked"),
    ("api.routes.agents", "ensure_not_capacity_locked"),
    ("api.routes.public_search", "ensure_not_capacity_locked"),
    ("api.routes.context_search_config", "ensure_context_not_capacity_locked"),
)


@pytest.fixture(autouse=True)
def _capacity_gated_routes_need_a_session(monkeypatch):
    """Run the routes' capacity-lock check only against a session (#1941).

    ``tests/api/test_capacity_lock_routes.py`` patches each name with a
    raising mock to pin where the check sits; that patch wins over this one.
    """
    import importlib

    for module_name, attr in _CAPACITY_GATED_ROUTES:
        module = importlib.import_module(module_name)
        real = getattr(module, attr)

        async def _gated(db, *args, _real=real, **kwargs):
            if isinstance(db, AsyncSession):
                await _real(db, *args, **kwargs)

        monkeypatch.setattr(module, attr, _gated)


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


@pytest.fixture(autouse=True)
def _clear_vocabulary_cache():
    """Reset the process-local write-lint tag vocabulary cache around every test (#1512).

    ``fetch_vocabulary_cached`` keeps a module-global ``TTLCache`` keyed by
    (workspace, context, scope). Without clearing it, a vocabulary mocked by one
    test would be served as a cache hit to a later test using the same fixture
    ids, making lint assertions order-dependent. Cheap (a dict clear).
    """
    from services.tag_resolution import clear_vocabulary_cache

    clear_vocabulary_cache()
    yield
    clear_vocabulary_cache()


@pytest.fixture
def no_identity_links():
    """No account is linked to another (#1784, #1807).

    The link-set lookups run one more ``db.execute`` than the scripted mock
    sessions of many unit modules expect: a private recall resolves the
    caller's link set, a private-context mismatch asks whether two ids are
    linked, a private tag vocabulary is keyed by the link set. Modules that
    script their sessions opt in with
    ``pytestmark = pytest.mark.usefixtures("no_identity_links")``.
    Real link behaviour is covered against Postgres in
    ``tests/integration/test_identity_links_db.py``.
    """
    from unittest.mock import patch

    async def only_self(_db, user_id):
        return frozenset({user_id})

    async def not_linked(_db, user_id, other_user_id):
        return other_user_id is not None and user_id == other_user_id

    with (
        patch("services.search_service.linked_user_ids", only_self),
        patch("services.tag_resolution.linked_user_ids", only_self),
        patch("services.permission_service.is_same_owner", not_linked),
    ):
        yield


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


def _test_database_required() -> bool:
    """Whether this run must have a usable test database (``REQUIRE_TEST_DATABASE=1``)."""
    return os.environ.get("REQUIRE_TEST_DATABASE") == "1"


def _test_database_unavailable(error: BaseException) -> NoReturn:
    """End the ``async_engine`` setup: skip, or fail when the database is required.

    The unit job and a local run without Postgres skip every ``db_session``
    test. A job whose whole point is those tests sets
    ``REQUIRE_TEST_DATABASE=1`` so that a schema the database rejects turns
    it red instead of green-by-skip (#1885).
    """
    if _test_database_required():
        pytest.fail(
            f"Test database not usable but REQUIRE_TEST_DATABASE=1: {error}",
            pytrace=False,
        )
    pytest.skip(f"Test database not available: {error}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine():
    """Create async engine for tests.

    Skips if the DB is unavailable, or fails under ``REQUIRE_TEST_DATABASE=1``.
    """
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
        _test_database_unavailable(e)

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
