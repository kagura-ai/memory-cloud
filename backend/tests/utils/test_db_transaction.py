"""``db_transaction``: what each kind of error becomes (#2443)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from utils.db_helpers import db_transaction
from utils.exceptions import ConflictError


def _session() -> MagicMock:
    db = MagicMock()
    db.rollback = AsyncMock()
    return db


async def test_a_memory_cloud_exception_is_rolled_back_and_re_raised_unchanged() -> None:
    """The global handler answers it with its own status and code (409
    ``RES-002`` here), not the wrapper's 500."""
    db = _session()
    error = ConflictError("Workspace is being modified; retry the operation")

    with pytest.raises(ConflictError) as raised:
        async with db_transaction(db, "op", "Failed"):
            raise error

    assert raised.value is error
    db.rollback.assert_awaited_once()


async def test_an_http_exception_passes_through_without_a_rollback() -> None:
    db = _session()
    error = HTTPException(status_code=404, detail="not found")

    with pytest.raises(HTTPException) as raised:
        async with db_transaction(db, "op", "Failed"):
            raise error

    assert raised.value is error
    db.rollback.assert_not_awaited()


async def test_any_other_error_is_rolled_back_and_becomes_a_500() -> None:
    db = _session()
    error = RuntimeError("boom")

    with pytest.raises(HTTPException) as raised:
        async with db_transaction(db, "op", "Failed to do op"):
            raise error

    assert raised.value.status_code == 500
    assert raised.value.detail == "Failed to do op"
    assert raised.value.__cause__ is error
    db.rollback.assert_awaited_once()
