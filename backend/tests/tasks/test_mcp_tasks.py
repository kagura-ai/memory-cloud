"""MCP session cleanup runs in every worker process (Issue #1980).

The session table is process-local, so the cleanup cannot be a scheduler job
(those run only on the scheduler leader). Each worker's lifespan starts
``start_local_session_cleanup`` and cancels it on shutdown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tasks import mcp_tasks


async def _until(predicate, steps: int = 200) -> None:
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_local_cleanup_runs_on_its_interval_and_stops_on_cancel():
    cleanup = AsyncMock()
    with patch.object(mcp_tasks, "cleanup_mcp_sessions_task", cleanup):
        task = mcp_tasks.start_local_session_cleanup(interval_seconds=0.01)
        await _until(lambda: cleanup.await_count >= 3)
        assert cleanup.await_count >= 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            assert await task is None
        count = cleanup.await_count
        await asyncio.sleep(0.05)
    assert task.cancelled()
    assert cleanup.await_count == count  # nothing runs after shutdown


@pytest.mark.asyncio
async def test_local_cleanup_survives_a_failing_run():
    cleanup = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
    with patch.object(mcp_tasks, "cleanup_mcp_sessions_task", cleanup):
        task = mcp_tasks.start_local_session_cleanup(interval_seconds=0.01)
        await _until(lambda: cleanup.await_count >= 2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            assert await task is None


@pytest.mark.asyncio
async def test_interval_defaults_to_the_env_var(monkeypatch):
    monkeypatch.setenv("MCP_SESSION_CLEANUP_INTERVAL", "3600")
    loop = AsyncMock()
    with patch.object(mcp_tasks, "_session_cleanup_loop", loop):
        task = mcp_tasks.start_local_session_cleanup()
        assert await task is None
    loop.assert_awaited_once_with(3600)


def test_lifespan_starts_and_cancels_the_cleanup_in_every_worker():
    """main.py starts the loop outside the leader path and cancels it on shutdown."""
    main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
    assert "app.state.mcp_session_cleanup_task = start_local_session_cleanup()" in main_src
    assert 'getattr(app.state, "mcp_session_cleanup_task", None)' in main_src
