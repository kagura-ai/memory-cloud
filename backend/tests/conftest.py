"""Pytest configuration and fixtures for Kagura Memory Cloud tests."""

import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from models.auth import Base as AuthBase
from models.memory import Base as MemoryBase

# Test database URL
# Use postgres service name in Docker, localhost for local testing
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://kagura:kagura_dev_password@postgres:5432/kagura_test",
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine():
    """Create async engine for tests."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        echo=False,
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
        await conn.run_sync(MemoryBase.metadata.create_all)

    yield engine

    # Cleanup
    try:
        # Drop all tables
        async with engine.begin() as conn:
            await conn.run_sync(MemoryBase.metadata.drop_all)
            await conn.run_sync(AuthBase.metadata.drop_all)
    except Exception:
        pass  # Ignore cleanup errors
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
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
