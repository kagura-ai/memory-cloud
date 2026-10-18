"""Tests for the embedding run's claim renewal (#2041).

The outcome writes of ``process_pending_embedding`` are fenced on the stamp the
run's claim holds, so a live run renews it; otherwise a run that is merely slow
would be reclaimed by the sweep, fenced out, and no run would ever finish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services import embedding_claim
from services.embedding_claim import EmbeddingClaim

_T0 = datetime(2026, 1, 2, 3, 4, 5, 678901)
_T1 = _T0 + timedelta(seconds=20)
_T2 = _T0 + timedelta(seconds=40)
_T3 = _T0 + timedelta(seconds=60)


def _row(owned: bool) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=uuid4() if owned else None)
    return result


class _Sessions:
    """``isolated_session`` stand-in: every renewal gets this one session."""

    def __init__(self, execute: Any) -> None:
        self.statements: list[Any] = []
        self.session = MagicMock()
        self.session.connection = AsyncMock()
        self.session.commit = AsyncMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.__aexit__ = AsyncMock(return_value=None)
        results = list(execute) if isinstance(execute, list) else None

        async def _execute(stmt: Any) -> Any:
            self.statements.append(stmt)
            if results is None:
                return await execute()
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.session.execute = AsyncMock(side_effect=_execute)

    def __call__(self, _db: Any) -> MagicMock:
        return self.session


def _claim() -> EmbeddingClaim:
    return EmbeddingClaim(MagicMock(), uuid4(), _T0)


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_poll(), timeout)


@pytest.fixture
def fast_renewal() -> Iterator[None]:
    with patch.object(embedding_claim, "RENEW_INTERVAL_SECONDS", 0.001):
        yield


@pytest.mark.asyncio
async def test_a_live_run_renews_its_stamp_until_the_claim_is_gone(fast_renewal):
    sessions = _Sessions([_row(True), _row(True), _row(False)])
    claim = _claim()
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", side_effect=[_T1, _T2, _T3]),
    ):
        claim.start_renewal()
        await _until(lambda: len(sessions.statements) == 3)
        await claim.stop_renewal()
    # The last renewal found the claim gone (re-queued or reclaimed): the
    # stamp stays the last one this run held, and renewing stops.
    assert claim.stamp == _T2
    assert len(sessions.statements) == 3


@pytest.mark.asyncio
async def test_a_failed_renewal_is_logged_and_retried(fast_renewal):
    sessions = _Sessions([RuntimeError("db blip"), _row(True), _row(False)])
    claim = _claim()
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", side_effect=[_T1, _T2, _T3]),
        patch.object(embedding_claim, "logger") as log,
    ):
        claim.start_renewal()
        await _until(lambda: len(sessions.statements) == 3)
        await claim.stop_renewal()
    assert claim.stamp == _T2
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "embedding_claim_renew_failed"
    assert log.warning.call_args.kwargs["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_no_renewal_while_the_stamp_is_held(fast_renewal):
    sessions = _Sessions([_row(True)] * 1000)
    claim = _claim()
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", return_value=_T1),
    ):
        claim.start_renewal()
        async with claim.held() as stamp:
            await asyncio.sleep(0.02)
            assert stamp == _T0
            assert claim.stamp == _T0
            assert sessions.statements == []
        await _until(lambda: len(sessions.statements) >= 1)
        await claim.stop_renewal()
    assert claim.stamp == _T1


@pytest.mark.asyncio
async def test_stop_waits_for_a_renewal_that_is_writing(fast_renewal):
    started, release = asyncio.Event(), asyncio.Event()

    async def _slow_write() -> MagicMock:
        started.set()
        await release.wait()
        return _row(True)

    sessions = _Sessions(_slow_write)
    claim = _claim()
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", return_value=_T1),
    ):
        claim.start_renewal()
        await asyncio.wait_for(started.wait(), 2)
        stopping = asyncio.create_task(claim.stop_renewal())
        await asyncio.sleep(0.01)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 2)
    # The stamp the renewal committed is the one the outcome write fences on.
    assert claim.stamp == _T1


@pytest.mark.asyncio
async def test_stop_cancels_a_renewal_waiting_for_a_connection(fast_renewal):
    waiting = asyncio.Event()

    async def _pool_exhausted() -> None:
        waiting.set()
        await asyncio.Event().wait()

    sessions = _Sessions([_row(True)])
    sessions.session.connection = AsyncMock(side_effect=_pool_exhausted)
    claim = _claim()
    with patch.object(embedding_claim, "isolated_session", sessions):
        claim.start_renewal()
        await asyncio.wait_for(waiting.wait(), 2)
        await asyncio.wait_for(claim.stop_renewal(), 1)
    assert sessions.statements == []
    assert claim.stamp == _T0


@pytest.mark.asyncio
async def test_a_run_past_the_max_is_no_longer_renewed(fast_renewal):
    sessions = _Sessions([_row(True)])
    claim = _claim()
    with (
        patch.object(embedding_claim, "MAX_RENEW_SECONDS", 0.0),
        patch.object(embedding_claim, "isolated_session", sessions),
        patch.object(embedding_claim, "logger") as log,
    ):
        claim.start_renewal()
        await _until(lambda: log.warning.called)
        await claim.stop_renewal()
    assert sessions.statements == []
    assert claim.stamp == _T0
    assert log.warning.call_args.args[0] == "embedding_claim_renewal_expired"


@pytest.mark.asyncio
async def test_stop_without_start_is_a_no_op():
    claim = _claim()
    await claim.stop_renewal()
    await claim.stop_renewal()
    claim.start_renewal()  # stopped for good: a late start does not renew
    assert claim._task is None


@pytest.mark.asyncio
async def test_the_renewal_is_fenced_on_the_current_stamp(fast_renewal):
    sessions = _Sessions([_row(True), _row(False)])
    claim = _claim()
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", side_effect=[_T1, _T2]),
    ):
        claim.start_renewal()
        await _until(lambda: len(sessions.statements) == 2)
        await claim.stop_renewal()

    def _sql(stmt: Any) -> str:
        compiled = stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
        return str(compiled).upper().replace("MEMORIES.", "")

    first, second = (_sql(s) for s in sessions.statements)
    set_part, where = first.split("WHERE", 1)
    assert "EMBEDDING_ATTEMPTED_AT='2026-01-02 03:04:25.678901'" in set_part
    assert "EMBEDDING_STATUS = 'PROCESSING'" in where
    assert "EMBEDDING_ATTEMPTED_AT = '2026-01-02 03:04:05.678901'" in where
    assert "UPDATED_AT" not in first
    # The next renewal is fenced on the stamp the first one wrote.
    assert "EMBEDDING_ATTEMPTED_AT = '2026-01-02 03:04:25.678901'" in second.split("WHERE", 1)[1]
    assert sessions.session.commit.await_count == 2


# #2070: a renewal that waits for a pooled connection waits at most
# RENEW_CHECKOUT_TIMEOUT_SECONDS, so a starved pool cannot turn one failed
# renewal into a reclaim.


@pytest.mark.asyncio
async def test_a_renewal_gives_up_on_a_slow_checkout_and_tries_again(fast_renewal):
    attempts = 0

    async def _pool_exhausted() -> None:
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()

    sessions = _Sessions([_row(True)])
    sessions.session.connection = AsyncMock(side_effect=_pool_exhausted)
    claim = _claim()
    with (
        patch.object(embedding_claim, "RENEW_CHECKOUT_TIMEOUT_SECONDS", 0.01),
        patch.object(embedding_claim, "isolated_session", sessions),
        patch.object(embedding_claim, "logger") as log,
    ):
        claim.start_renewal()
        await _until(lambda: attempts >= 2)
        await claim.stop_renewal()
    assert sessions.statements == []
    assert claim.stamp == _T0
    first = log.warning.call_args_list[0]
    assert first.args[0] == "embedding_claim_renew_failed"
    assert first.kwargs["error_class"] == "TimeoutError"


def test_two_failed_renewals_still_leave_a_third_before_the_stale_cutoff():
    # A renewal that started at T; the next three start at T+15, T+30, T+45 and
    # each has its connection, or gives up, within 5 s.
    interval = embedding_claim.RENEW_INTERVAL_SECONDS
    checkout = embedding_claim.RENEW_CHECKOUT_TIMEOUT_SECONDS
    third_connected_by = 3 * interval + checkout
    assert third_connected_by < embedding_claim.STALE_CLAIM_SECONDS


@pytest.mark.asyncio
async def test_a_slow_checkout_does_not_push_the_next_renewal_back():
    starts: list[float] = []

    async def _pool_exhausted() -> None:
        starts.append(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    sessions = _Sessions([_row(True)])
    sessions.session.connection = AsyncMock(side_effect=_pool_exhausted)
    claim = _claim()
    with (
        patch.object(embedding_claim, "RENEW_INTERVAL_SECONDS", 0.4),
        patch.object(embedding_claim, "RENEW_CHECKOUT_TIMEOUT_SECONDS", 0.3),
        patch.object(embedding_claim, "isolated_session", sessions),
        patch.object(embedding_claim, "logger"),
    ):
        claim.start_renewal()
        await _until(lambda: len(starts) >= 3, timeout=10.0)
        await claim.stop_renewal()
    # One interval apart, not interval + checkout wait (0.7).
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert all(gap < 0.55 for gap in gaps), gaps


# #2070: ``embedding_claim_lost`` says why the claim went. A row is reclaimable
# only once its stamp is STALE_CLAIM_SECONDS old, so a loss seen earlier is a
# re-queue (an edit, a migration switch) or a delete.


@pytest.mark.asyncio
async def test_a_loss_the_renewal_saw_while_the_stamp_was_fresh_is_a_requeue(fast_renewal):
    sessions = _Sessions([_row(False)])
    claim = _claim()
    late = _T0 + timedelta(seconds=300)
    with (
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", side_effect=[_T1, late]),
    ):
        claim.start_renewal()
        await _until(lambda: len(sessions.statements) == 1)
        await claim.stop_renewal()
        # Seen by the renewal at T+20; the run noticing it much later does not
        # make it stale.
        assert claim.loss_reason() == "requeued"


@pytest.mark.asyncio
async def test_a_loss_seen_with_a_fresh_stamp_is_a_requeue():
    claim = _claim()
    with patch("utils.datetime.utcnow", return_value=_T0 + timedelta(seconds=59)):
        assert claim.loss_reason() == "requeued"


@pytest.mark.asyncio
async def test_a_loss_seen_with_a_stale_stamp_is_a_possible_reclaim():
    claim = _claim()
    with patch("utils.datetime.utcnow", return_value=_T0 + timedelta(seconds=60)):
        assert claim.loss_reason() == "stale"


@pytest.mark.asyncio
async def test_stop_ends_the_loop_when_renewals_overrun_the_interval():
    """#2070: an attempt longer than the interval leaves no wait before the
    next one, and the loop must still see a stop that arrived during it (the
    stop does not cancel a renewal that is writing)."""
    writing = asyncio.Event()

    async def _slow_write() -> MagicMock:
        writing.set()
        await asyncio.sleep(0.02)
        return _row(True)

    async def _slow_checkout() -> None:
        await asyncio.sleep(0.01)

    sessions = _Sessions(_slow_write)
    # Every attempt, even one that only finds the stop, outlasts the interval.
    sessions.session.connection = AsyncMock(side_effect=_slow_checkout)
    claim = _claim()
    with (
        patch.object(embedding_claim, "RENEW_INTERVAL_SECONDS", 0.001),
        patch.object(embedding_claim, "isolated_session", sessions),
        patch("utils.datetime.utcnow", return_value=_T1),
    ):
        claim.start_renewal()
        await asyncio.wait_for(writing.wait(), 2)
        await asyncio.wait_for(claim.stop_renewal(), 1)
    # The write that was under way when the stop came is the last one.
    assert len(sessions.statements) == 1
