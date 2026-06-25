"""Unit tests for the batched eval-ingest indexing barrier.

The retrieval eval ingests the whole corpus, then drives embeddings concurrently
and polls the WHOLE set in one barrier (``_index_corpus`` / ``_await_all_indexed``
/ ``_drive_pending_embeddings`` / ``_reset_pending``) instead of the old per-doc
``drive -> 30s-poll`` serial loop. These tests pin that orchestration DB-free
(fake svc/db), so they run in the plain unit suite — no live stack, no embeddings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.eval import runner


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _FakeDB:
    """Returns ``(id, status, error)`` for every tracked memory on each
    ``execute()``. ``statuses`` may be mutated between polls to simulate progress.
    """

    def __init__(self, statuses: dict, errors: dict | None = None) -> None:
        self.statuses = statuses
        self.errors = errors or {}

    async def execute(self, _stmt):  # noqa: ANN001 — stmt shape irrelevant to the fake
        return _FakeResult(
            [(mid, self.statuses[mid], self.errors.get(mid)) for mid in self.statuses]
        )

    async def commit(self) -> None:
        return None


class _FakeSvc:
    def __init__(self, statuses: dict, errors: dict | None = None) -> None:
        self.db = _FakeDB(statuses, errors)


class TestAwaitAllIndexed:
    """The single-query terminal barrier."""

    async def test_all_success_returns_empty(self):
        ids = [uuid4() for _ in range(5)]
        svc = _FakeSvc(dict.fromkeys(ids, "success"))
        assert await runner._await_all_indexed(svc, ids, timeout_s=1.0) == {}

    async def test_collects_failed_without_raising(self):
        ids = [uuid4() for _ in range(3)]
        bad = ids[1]
        statuses = dict.fromkeys(ids, "success") | {bad: "failed"}
        svc = _FakeSvc(statuses, errors={bad: "boom"})
        # A failed doc is RETURNED (so the caller can reset-and-retry), not raised,
        # and the successful docs are not lost.
        assert await runner._await_all_indexed(svc, ids, timeout_s=1.0) == {bad: "boom"}

    async def test_waits_then_succeeds(self):
        ids = [uuid4() for _ in range(2)]
        statuses = dict.fromkeys(ids, "pending")
        svc = _FakeSvc(statuses)
        calls = {"n": 0}
        inner = svc.db.execute

        async def _exec(stmt):
            calls["n"] += 1
            if calls["n"] >= 2:  # flip to terminal after the first poll
                for i in ids:
                    statuses[i] = "success"
            return await inner(stmt)

        svc.db.execute = _exec
        assert await runner._await_all_indexed(svc, ids, timeout_s=1.0, interval_s=0.01) == {}
        assert calls["n"] >= 2  # it actually polled more than once

    async def test_timeout_raises(self):
        ids = [uuid4()]
        svc = _FakeSvc({ids[0]: "pending"})  # never becomes terminal
        with pytest.raises(RuntimeError, match="never reached a terminal state"):
            await runner._await_all_indexed(svc, ids, timeout_s=0.03, interval_s=0.01)


class TestIndexCorpus:
    """The drive -> poll -> bounded reset-and-retry orchestration."""

    async def test_success_first_try(self, monkeypatch):
        ids = [uuid4() for _ in range(4)]
        drive = AsyncMock()
        reset = AsyncMock()
        monkeypatch.setattr(runner, "_drive_pending_embeddings", drive)
        monkeypatch.setattr(runner, "_await_all_indexed", AsyncMock(return_value={}))
        monkeypatch.setattr(runner, "_reset_pending", reset)

        await runner._index_corpus(_FakeSvc({}), ids)

        drive.assert_awaited_once()
        assert list(drive.await_args.args[1]) == ids  # drove the whole corpus
        reset.assert_not_awaited()  # nothing failed -> no reset

    async def test_retries_failed_then_succeeds(self, monkeypatch):
        ids = [uuid4() for _ in range(3)]
        failed_once = {ids[0]: "transient"}
        drive = AsyncMock()
        reset = AsyncMock()
        await_mock = AsyncMock(side_effect=[failed_once, {}])  # fail once, then clean
        monkeypatch.setattr(runner, "_drive_pending_embeddings", drive)
        monkeypatch.setattr(runner, "_await_all_indexed", await_mock)
        monkeypatch.setattr(runner, "_reset_pending", reset)

        await runner._index_corpus(_FakeSvc({}), ids)

        assert await_mock.await_count == 2
        assert drive.await_count == 2
        reset.assert_awaited_once()
        # the retry resets + re-drives ONLY the failed doc, not the whole corpus
        assert list(reset.await_args.args[1]) == [ids[0]]
        assert list(drive.await_args.args[1]) == [ids[0]]

    async def test_persistent_failure_raises_after_retries(self, monkeypatch):
        ids = [uuid4()]
        await_mock = AsyncMock(return_value={ids[0]: "perma"})  # always fails
        monkeypatch.setattr(runner, "_drive_pending_embeddings", AsyncMock())
        monkeypatch.setattr(runner, "_await_all_indexed", await_mock)
        monkeypatch.setattr(runner, "_reset_pending", AsyncMock())

        with pytest.raises(RuntimeError, match="embedding FAILED"):
            await runner._index_corpus(_FakeSvc({}), ids)

        # one initial attempt + _INGEST_RETRIES retries
        assert await_mock.await_count == runner._INGEST_RETRIES + 1
