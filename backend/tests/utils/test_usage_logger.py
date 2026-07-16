"""Tests for the shared usage logger (#1318).

An MCP tool called with a nonexistent context_id returns a clean 404 to the
client, but the usage row INSERT then violates the usage_stats→contexts FK.
log_usage must preserve the usage/quota event (context attribution dropped)
instead of dropping the whole row.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from utils.usage_logger import log_usage


def _fk_error() -> IntegrityError:
    return IntegrityError("INSERT INTO usage_stats ...", {}, Exception("fk violation"))


def _db(execute_side_effect) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_success_returns_true():
    db = _db([None])
    assert await log_usage(db, "u", "mcp:recall", context_id="ctx") is True
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_fk_violation_falls_back_to_null_context():
    """#1318: FK violation on a stamped context_id retries with NULL."""
    db = _db([_fk_error(), None])

    result = await log_usage(
        db,
        "u",
        "mcp:get_context_info",
        status_code=404,
        context_id="00000000-0000-0000-0000-000000000000",
    )

    assert result is True
    assert db.execute.await_count == 2
    retry_stmt = db.execute.await_args_list[1].args[0]
    assert retry_stmt.compile().params["context_id"] is None
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_fk_violation_without_context_returns_false():
    """No context to drop — the IntegrityError is a real failure."""
    db = _db([_fk_error()])
    assert await log_usage(db, "u", "mcp:recall", context_id=None) is False
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_retry_failure_returns_false():
    db = _db([_fk_error(), RuntimeError("db down")])
    result = await log_usage(db, "u", "mcp:recall", context_id="ctx")
    assert result is False
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
async def test_generic_failure_returns_false():
    db = _db([RuntimeError("boom")])
    assert await log_usage(db, "u", "mcp:recall", context_id="ctx") is False
    db.rollback.assert_awaited_once()
