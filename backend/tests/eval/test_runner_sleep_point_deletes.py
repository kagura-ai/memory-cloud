"""The eval runners' Sleep step deletes the run's points after its commit (#2565).

The production callers (the nightly task, the admin trigger) call
``SleepOrchestrator.delete_committed_points`` right after their commit, and no
drain job runs in the harness, so the replay and update runners do the same.
The orchestrator and the config are stubbed: no DB, no stack.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from neural.config import NeuralMemoryConfig
from tests.eval import replay_runner, update_runner

RunSleep = Callable[[Any], Coroutine[Any, Any, dict[str, Any]]]


async def _replay(db: Any) -> dict[str, Any]:
    ids = SimpleNamespace(id=uuid4())
    return await replay_runner._run_sleep(db, "owner", ids, ids)


async def _update(db: Any) -> dict[str, Any]:
    return await update_runner._run_mc_sleep(db, "owner", uuid4(), uuid4())


RUNNERS = pytest.mark.parametrize("run_sleep", [_replay, _update], ids=["replay", "update"])


def _db(order: list[str], *, commit_fails: bool = False) -> MagicMock:
    async def commit() -> None:
        order.append("commit")
        if commit_fails:
            raise RuntimeError("commit failed")

    db = MagicMock()
    db.commit = AsyncMock(side_effect=commit)
    db.rollback = AsyncMock(side_effect=lambda: order.append("rollback"))
    # The report lookup after the run finds none.
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    return db


@contextmanager
def _orchestrator(order: list[str]) -> Iterator[None]:
    orchestrator = MagicMock()
    orchestrator.run = AsyncMock(side_effect=lambda *_, **__: order.append("run"))
    orchestrator.delete_committed_points = AsyncMock(side_effect=lambda: order.append("points"))
    with (
        patch(
            "services.sleep.orchestrator.SleepOrchestrator",
            MagicMock(return_value=orchestrator),
        ),
        patch.object(NeuralMemoryConfig, "from_db", AsyncMock(return_value=NeuralMemoryConfig())),
        patch.object(NeuralMemoryConfig, "invalidate_cache", MagicMock()),
    ):
        yield


@RUNNERS
async def test_the_points_are_deleted_after_the_commit(run_sleep: RunSleep) -> None:
    order: list[str] = []
    with _orchestrator(order):
        await run_sleep(_db(order))

    assert order == ["run", "commit", "points"]


@RUNNERS
async def test_a_failed_commit_deletes_nothing(run_sleep: RunSleep) -> None:
    order: list[str] = []
    with _orchestrator(order):
        result = await run_sleep(_db(order, commit_fails=True))

    assert order == ["run", "commit", "rollback"]
    assert result["ok"] is False
