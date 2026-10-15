"""#1990: the startup pass takes the advisory lock, runs the reconcile, never raises."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tasks import dense_vector_config as task_module
from tasks.dense_vector_config import (
    DENSE_VECTOR_CONFIG_LOCK_KEY,
    run_dense_vector_config_reconcile,
)


@pytest.fixture
def db(monkeypatch):
    session = AsyncMock()

    async def _get_db():
        yield session

    monkeypatch.setattr(task_module, "get_db", _get_db)
    return session


@pytest.fixture
def apply_all(monkeypatch):
    fake = AsyncMock(return_value={"kagura_memories": True, "kagura_memories_x_8": False})
    monkeypatch.setattr(task_module, "apply_dense_vector_config_to_all", fake)
    return fake


async def test_lock_then_apply_then_release(db, apply_all):
    results = await run_dense_vector_config_reconcile()

    assert results == {"kagura_memories": True, "kagura_memories_x_8": False}
    db.execute.assert_awaited_once()
    sql, params = db.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(sql)
    assert params == {"key": DENSE_VECTOR_CONFIG_LOCK_KEY}
    apply_all.assert_awaited_once()
    db.rollback.assert_awaited_once()


async def test_lock_is_released_when_the_pass_fails(db, apply_all, monkeypatch):
    apply_all.side_effect = RuntimeError("qdrant down")
    warnings: list[dict] = []
    monkeypatch.setattr(
        task_module.logger, "warning", lambda event, **kw: warnings.append({"event": event, **kw})
    )

    assert await run_dense_vector_config_reconcile() == {}

    db.rollback.assert_awaited_once()
    assert [w["event"] for w in warnings] == ["dense_vector_config_apply_failed"]
    assert "qdrant down" in warnings[0]["error"]


async def test_database_failure_is_logged_not_raised(apply_all, monkeypatch):
    async def _broken():
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(task_module, "get_db", _broken)
    warnings: list[str] = []
    monkeypatch.setattr(task_module.logger, "warning", lambda event, **kw: warnings.append(event))

    assert await run_dense_vector_config_reconcile() == {}

    apply_all.assert_not_awaited()
    assert warnings == ["dense_vector_config_apply_failed"]
