"""Async task entry point for Memory Analysis runs.

Mirrors ``backend/src/tasks/sleep_tasks.py``'s shape but does NOT
register an APScheduler cron job — analysis is on-demand only.
The exposed coroutine ``run_analysis_task`` is invoked by the B3
API layer (#496) via ``asyncio.create_task`` after the orchestrator's
``start()`` step has committed the ``memory_analyses`` row at
``status='running'`` and the 202 response has been returned.

Task lifecycle:

1. API handler synchronously calls ``AnalysisOrchestrator.start()``
   on the request session. That returns a ``MemoryAnalysis`` row at
   ``status='running'``. The handler commits and returns 202 with
   ``run_id``.
2. API handler kicks off this task with the run_id. The task opens
   a **fresh** DB session (the request session is gone by the time
   the task runs) and calls ``AnalysisOrchestrator.run()``.
3. ``run()`` either succeeds (status='succeeded') or raises (status
   set to 'failed' inside ``run()`` itself before the exception
   propagates out). Either way the run row reaches a terminal state
   the API client can poll.

The task swallows ``Exception`` at the outer boundary because no
caller awaits it — exceptions would otherwise propagate to the
asyncio event loop's default handler and clutter logs without
helping observability. Failures are already persisted to
``memory_analyses.error``; the log line here is the breadcrumb that
ties the failure to a worker invocation.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from db.base import get_db
from services.analysis.orchestrator import AnalysisOrchestrator
from utils.logger import get_logger

logger = get_logger(__name__)

# #1241: in-process registry of live run tasks so a soft-cancel can also
# stop the compute (and its BYOK spend), not just flip the row. Keyed by
# ``memory_analyses.id``. Lives here — not in the REST module — because
# both kickoff surfaces (api/routes/analyses.py and
# mcp_server/tools/analysis.py) spawn ``run_analysis_task`` and the REST
# DELETE handler must be able to cancel an MCP-started run too.
# Best-effort by design: in a multi-process deployment the task may live
# in another worker, in which case the reporter's locked cancellation
# guard still prevents any post-cancel persist — only that worker's
# in-flight LLM calls run to completion.
_RUN_TASKS: dict[UUID, asyncio.Task[None]] = {}


def register_run_task(run_id: UUID, task: asyncio.Task[None]) -> None:
    """Track a spawned run task so ``cancel_run_task`` can find it.

    The done-callback removes the entry regardless of outcome, so the
    registry only ever holds live tasks.
    """
    _RUN_TASKS[run_id] = task
    task.add_done_callback(lambda _t: _RUN_TASKS.pop(run_id, None))


def cancel_run_task(run_id: UUID) -> bool:
    """Cancel the in-process task for ``run_id`` if it is still live.

    Returns True when a live task was found and cancellation was
    requested; False when no task is registered in this process (other
    worker, already finished, or crashed run).
    """
    task = _RUN_TASKS.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    logger.info("analysis_run_task_cancel_requested", analysis_id=str(run_id))
    return True


async def run_analysis_task(analysis_id: UUID) -> None:
    """Run the Memory Analysis pipeline for one ``memory_analyses`` row.

    Args:
        analysis_id: The UUID of the row already at status='running'.
            Created by ``AnalysisOrchestrator.start()`` in the API
            handler before the 202 response was returned.
    """
    logger.info("analysis_task_started", analysis_id=str(analysis_id))
    try:
        async for db in get_db():
            orchestrator = AnalysisOrchestrator(db)
            await orchestrator.run(analysis_id=analysis_id)
    except asyncio.CancelledError:
        # #1241: DELETE soft-cancel flipped the row (status='cancelled'
        # committed under a row lock) and then cancelled this task.
        # Nothing to persist — re-raise so the task ends in the
        # cancelled state (`task.cancelled()` is True for the done
        # callback).
        logger.info("analysis_task_cancelled", analysis_id=str(analysis_id))
        raise
    except Exception as e:  # noqa: BLE001
        # Failure path is already persisted by the orchestrator's
        # internal _mark_failed. This catch keeps the task from
        # surfacing as an unawaited-exception warning.
        logger.error(
            "analysis_task_failed",
            analysis_id=str(analysis_id),
            error=str(e),
            exc_info=True,
        )
    else:
        logger.info("analysis_task_complete", analysis_id=str(analysis_id))
