"""``retry_nowait``: a ``NOWAIT`` row lock tried again under a savepoint when
the row is busy, then a 409 (#2443). The two-session behaviour is in
``tests/integration/test_erasure_lock_order_db.py``."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError

from services.workspace_locks import (
    NOWAIT_LOCK_BACKOFF_SECONDS,
    NOWAIT_LOCK_TRIES,
    retry_nowait,
)
from utils.exceptions import ConflictError


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT ... FOR UPDATE NOWAIT", {}, _DriverError(sqlstate))


def _session() -> tuple[MagicMock, list[str]]:
    """A session whose ``begin_nested`` records each savepoint and how it ended."""
    savepoints: list[str] = []

    @asynccontextmanager
    async def begin_nested():
        try:
            yield
        except BaseException:
            savepoints.append("rolled back")
            raise
        savepoints.append("released")

    db = MagicMock()
    db.begin_nested = begin_nested
    return db, savepoints


async def test_a_row_busy_on_every_try_is_a_conflict_after_the_last() -> None:
    db, savepoints = _session()
    lock = AsyncMock(side_effect=_db_error("55P03"))
    sleep = AsyncMock()

    with patch("services.workspace_locks.asyncio.sleep", sleep), pytest.raises(ConflictError):
        await retry_nowait(db, lock)

    assert NOWAIT_LOCK_TRIES == 5
    assert lock.await_count == NOWAIT_LOCK_TRIES
    assert savepoints == ["rolled back"] * NOWAIT_LOCK_TRIES
    # 0.05 + 0.1 + 0.2 + 0.4 s between the tries, none after the last
    assert [call.args[0] for call in sleep.await_args_list] == [
        NOWAIT_LOCK_BACKOFF_SECONDS * 2**attempt for attempt in range(NOWAIT_LOCK_TRIES - 1)
    ]
    assert NOWAIT_LOCK_BACKOFF_SECONDS == 0.05


async def test_a_row_freed_between_tries_is_locked() -> None:
    db, savepoints = _session()
    lock = AsyncMock(side_effect=[_db_error("55P03"), "locked"])
    sleep = AsyncMock()

    with patch("services.workspace_locks.asyncio.sleep", sleep):
        assert await retry_nowait(db, lock) == "locked"

    assert savepoints == ["rolled back", "released"]
    assert sleep.await_count == 1


async def test_another_database_error_is_not_retried() -> None:
    db, savepoints = _session()
    error = _db_error("23505")
    lock = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    with (
        patch("services.workspace_locks.asyncio.sleep", sleep),
        pytest.raises(DBAPIError) as raised,
    ):
        await retry_nowait(db, lock)

    assert raised.value is error
    assert lock.await_count == 1
    assert savepoints == ["rolled back"]
    sleep.assert_not_awaited()
