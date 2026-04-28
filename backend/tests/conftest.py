"""Pytest configuration and fixtures for Kagura Memory Cloud tests."""

import os
from collections.abc import AsyncGenerator

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
import models.sleep  # noqa: F401  isort: skip


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
