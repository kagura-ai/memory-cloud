"""Workspace slot sweep (Issue #2918).

Every 15 minutes, while ``WORKSPACE_SLOT_SUSPENSION`` is ``shadow`` or
``enforce``: ``services.workspace_slots.sweep_workspace_slots`` visits the
owners with suspension rows in bounded batches, one after another, for up to
about a minute per run (the next run continues where this one stopped), tries
each owner's slot lock once and resumes and prunes; it never suspends. It finishes what a deferred or lost post-commit reconcile
left (a workspace deleted or transferred, a non-decreasing billing change
that found the lock busy) and removes rows of workspaces that are gone.
Single-flight across API processes; does nothing in mode ``off``.
"""

from __future__ import annotations

from time import monotonic

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db.base import get_db
from services.workspace_slots import sweep_workspace_slots, workspace_slot_mode
from tasks.job_runs import JobResult, job_failed
from tasks.single_flight import single_flight
from utils.logger import get_logger

logger = get_logger(__name__)

JOB_ID = "workspace_slot_sweep"
SWEEP_INTERVAL_MINUTES = 15
# How long one run keeps taking batches. Every owner with rows is visited once
# per run unless there are more than one minute's worth; then the next run
# continues after the last one visited.
SWEEP_TIME_BUDGET_SECONDS = 60.0
_LOCK_KEY = "workspace_slot_sweep"

# Where the next run continues (an owner id; "" = from the start). Per
# process: a new scheduler leader starts from the beginning.
_cursor = {"after": ""}


async def workspace_slot_sweep() -> JobResult:
    """The scheduled job: batches for up to a minute; integer counts or ``job_failed``."""
    if workspace_slot_mode() == "off":
        return {"skipped": 1}
    try:
        async with single_flight(_LOCK_KEY) as acquired:
            if not acquired:
                logger.info("workspace_slot_sweep_skipped", reason="single_flight")
                return {"skipped": 1}
            counts: dict[str, int] = dict.fromkeys(
                ("owners", "resumed", "pruned", "busy", "failed", "batches"), 0
            )
            started = monotonic()
            async for db in get_db():
                # One full pass at most: from the cursor to the last owner,
                # then from the first owner up to the cursor; never the same
                # owner twice in a run. Out of time: the next run continues.
                first = _cursor["after"]
                after, second_lap = first, False
                while monotonic() - started < SWEEP_TIME_BUDGET_SECONDS:
                    outcome = await sweep_workspace_slots(
                        db, after=after, until=first if second_lap else None
                    )
                    after = outcome.next_after
                    _cursor["after"] = after
                    counts["batches"] += 1
                    counts["owners"] += outcome.owners
                    counts["resumed"] += outcome.resumed
                    counts["pruned"] += outcome.pruned
                    counts["busy"] += outcome.busy
                    counts["failed"] += outcome.failed
                    if second_lap and after == first:
                        after = ""  # the second lap reached the cursor
                    if after == "":
                        if second_lap or first == "":
                            _cursor["after"] = ""
                            break  # a full pass
                        second_lap = True
            logger.info("workspace_slot_sweep_done", **counts)
            return counts
    except Exception as exc:
        logger.warning("workspace_slot_sweep_failed", error_class=type(exc).__name__)
        return job_failed(exc)


def schedule_workspace_slot_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the sweep (every 15 minutes), only while the mode is not ``off``.

    The job checks the mode again when it fires.
    """
    if workspace_slot_mode() == "off":
        logger.info("workspace_slot_sweep_disabled")
        return
    scheduler.add_job(
        workspace_slot_sweep,
        trigger=IntervalTrigger(minutes=SWEEP_INTERVAL_MINUTES),
        id=JOB_ID,
        name="Workspace slot sweep: resume and prune (#2918)",
        replace_existing=True,
    )
    logger.info("scheduled_workspace_slot_tasks", interval_minutes=SWEEP_INTERVAL_MINUTES)
