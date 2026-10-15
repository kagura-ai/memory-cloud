"""#1990: the startup pass runs in one worker under single_flight and never raises."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tasks import dense_vector_config as task_module
from tasks.dense_vector_config import (
    DENSE_VECTOR_CONFIG_LOCK_KEY,
    run_dense_vector_config_reconcile,
)


@pytest.fixture
def guard(monkeypatch):
    """Replace single_flight with a fake that records the key and yields `acquired`."""
    seen: dict[str, Any] = {"acquired": True, "keys": []}

    @asynccontextmanager
    async def _single_flight(lock_key: str) -> AsyncGenerator[bool]:
        seen["keys"].append(lock_key)
        yield bool(seen["acquired"])

    monkeypatch.setattr(task_module, "single_flight", _single_flight)
    return seen


@pytest.fixture
def apply_all(monkeypatch):
    fake = AsyncMock(return_value={"kagura_memories": True, "kagura_memories_x_8": False})
    monkeypatch.setattr(task_module, "apply_dense_vector_config_to_all", fake)
    return fake


@pytest.fixture
def events(monkeypatch):
    seen: list[dict] = []
    for level in ("info", "warning"):
        monkeypatch.setattr(
            task_module.logger,
            level,
            lambda event, _level=level, **kw: seen.append({"level": _level, "event": event, **kw}),
        )
    return seen


async def test_leader_runs_the_pass_under_the_lock(guard, apply_all, events):
    results = await run_dense_vector_config_reconcile()

    assert results == {"kagura_memories": True, "kagura_memories_x_8": False}
    assert guard["keys"] == [DENSE_VECTOR_CONFIG_LOCK_KEY]
    apply_all.assert_awaited_once()
    assert [e["event"] for e in events] == ["dense_vector_config_reconciled"]
    assert events[0]["changed"] == ["kagura_memories"]


async def test_other_workers_skip_when_the_lock_is_held(guard, apply_all, events):
    guard["acquired"] = False

    assert await run_dense_vector_config_reconcile() == {}

    apply_all.assert_not_awaited()
    assert [e["event"] for e in events] == ["dense_vector_config_skipped_busy"]


async def test_pass_failure_is_logged_not_raised(guard, apply_all, events):
    apply_all.side_effect = RuntimeError("qdrant down")

    assert await run_dense_vector_config_reconcile() == {}

    assert [(e["level"], e["event"]) for e in events] == [
        ("warning", "dense_vector_config_apply_failed")
    ]
    assert "qdrant down" in events[0]["error"]


async def test_lock_failure_is_logged_not_raised(apply_all, events, monkeypatch):
    @asynccontextmanager
    async def _broken(lock_key: str) -> AsyncGenerator[bool]:
        raise RuntimeError("pool exhausted")
        yield True  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(task_module, "single_flight", _broken)

    assert await run_dense_vector_config_reconcile() == {}

    apply_all.assert_not_awaited()
    assert [e["event"] for e in events] == ["dense_vector_config_apply_failed"]
