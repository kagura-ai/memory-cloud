"""Tests for the embedding run's claim renewal (#2041).

The outcome writes of ``process_pending_embedding`` are fenced on the stamp the
run's claim wrote, so a live run renews it; otherwise a run that is merely slow
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
    claim = _claim()
    renew = AsyncMock(side_effect=[_T1, _T2, None])
    with patch.object(claim, "_renew", renew):
        claim.start_renewal()
        await _until(lambda: renew.await_count == 3)
        await claim.stop_renewal()
    # The last renewal found the claim gone: the stamp stays the last one held.
    assert claim.stamp == _T2
    assert renew.await_count == 3


@pytest.mark.asyncio
async def test_a_failed_renewal_is_logged_and_retried(fast_renewal):
    claim = _claim()
    renew = AsyncMock(side_effect=[RuntimeError("db blip"), _T1, None])
    with patch.object(claim, "_renew", renew), patch.object(embedding_claim, "logger") as log:
        claim.start_renewal()
        await _until(lambda: renew.await_count == 3)
        await claim.stop_renewal()
    assert claim.stamp == _T1
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "embedding_claim_renew_failed"
    assert log.warning.call_args.kwargs["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_no_renewal_while_the_stamp_is_held(fast_renewal):
    claim = _claim()
    renew = AsyncMock(return_value=_T1)
    with patch.object(claim, "_renew", renew):
        claim.start_renewal()
        async with claim.held() as stamp:
            await asyncio.sleep(0.02)
            assert stamp == _T0
            assert claim.stamp == _T0
            renew.assert_not_awaited()
        await _until(lambda: renew.await_count >= 1)
        await claim.stop_renewal()
    assert claim.stamp == _T1


@pytest.mark.asyncio
async def test_stop_waits_for_a_renewal_in_flight(fast_renewal):
    claim = _claim()
    started, release = asyncio.Event(), asyncio.Event()

    async def _slow_renew() -> datetime:
        started.set()
        await release.wait()
        return _T1

    with patch.object(claim, "_renew", _slow_renew):
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
async def test_a_run_past_the_max_is_no_longer_renewed(fast_renewal):
    claim = _claim()
    renew = AsyncMock(return_value=_T1)
    with (
        patch.object(embedding_claim, "MAX_RENEW_SECONDS", 0.0),
        patch.object(claim, "_renew", renew),
        patch.object(embedding_claim, "logger") as log,
    ):
        claim.start_renewal()
        await _until(lambda: log.warning.called)
        await claim.stop_renewal()
    renew.assert_not_awaited()
    assert claim.stamp == _T0
    assert log.warning.call_args.args[0] == "embedding_claim_renewal_expired"


@pytest.mark.asyncio
async def test_stop_without_start_is_a_no_op():
    claim = _claim()
    await claim.stop_renewal()
    await claim.stop_renewal()
    claim.start_renewal()  # stopped for good: a late start does not renew
    assert claim._task is None


@pytest.mark.parametrize("matched", [True, False])
@pytest.mark.asyncio
async def test_renew_is_fenced_on_the_current_stamp(matched: bool):
    captured: list = []
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=uuid4() if matched else None)

    async def _execute(stmt: Any) -> MagicMock:
        captured.append(stmt)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    claim = _claim()

    with (
        patch.object(embedding_claim, "isolated_session", return_value=session),
        patch("utils.datetime.utcnow", return_value=_T1),
    ):
        renewed = await claim._renew()

    assert renewed == (_T1 if matched else None)
    session.commit.assert_awaited_once()
    sql = (
        str(
            captured[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        .upper()
        .replace("MEMORIES.", "")
    )
    set_part, where = sql.split("WHERE", 1)
    assert "EMBEDDING_ATTEMPTED_AT='2026-01-02 03:04:25.678901'" in set_part
    assert "EMBEDDING_STATUS = 'PROCESSING'" in where
    assert "EMBEDDING_ATTEMPTED_AT = '2026-01-02 03:04:05.678901'" in where
    assert "UPDATED_AT" not in sql
