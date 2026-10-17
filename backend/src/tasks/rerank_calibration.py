"""Startup rerank calibration task (Issue #2020).

Every API worker runs the lifespan; each one scores the /v1/rerank model once
in the background (``run_rerank_calibration_check``) so a mis-formatted Qwen3
reranker surfaces as ``rerank_model_uncalibrated`` instead of silently wrecking
recall. Started only when the check applies (``should_run_rerank_calibration``)
and cancelled at shutdown if it is still waiting on a reranker that has not
come up.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from services.reranker_service import (
    run_rerank_calibration_check,
    should_run_rerank_calibration,
)

if TYPE_CHECKING:
    from config.settings import Settings


def start_rerank_calibration(settings: Settings) -> asyncio.Task[object] | None:
    """Start the background check, or return ``None`` when it does not apply.

    The caller holds the returned task strongly (the loop keeps tasks only
    weakly) and passes it to :func:`stop_rerank_calibration` at shutdown.
    """
    if not should_run_rerank_calibration(settings):
        return None
    return asyncio.create_task(run_rerank_calibration_check(settings))


async def stop_rerank_calibration(task: asyncio.Task[object] | None) -> None:
    """Cancel the check if it is still running; a no-op for None or a done task."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # expected: we cancelled it ourselves
