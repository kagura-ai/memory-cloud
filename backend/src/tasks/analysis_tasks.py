"""Async task entry point for broadlistening analysis runs.

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

from uuid import UUID

from db.base import get_db
from services.analysis.orchestrator import AnalysisOrchestrator
from utils.logger import get_logger

logger = get_logger(__name__)


async def run_analysis_task(analysis_id: UUID) -> None:
    """Run the broadlistening pipeline for one ``memory_analyses`` row.

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
