"""Scheduler job of verified custom domains (#2809).

``public_mcp_domain_maintenance`` (every ``MAINTENANCE_INTERVAL_SECONDS``,
and once right at scheduler start) runs
:func:`services.public_mcp_domain_maintenance.run_maintenance`: housekeeping,
then the proof, right and lease check of every due held row. It is
registered only while ``MCP_PUBLIC_DOMAINS_ENABLED`` is on, and does nothing
if the switch is off when it fires.

Without a running scheduler leader no check completes, and every alias stops
when its 72-hour lease passes, under either authority.

The job returns integer counts (published to operators by the job status
route) and ``job_failed(exc)`` from its top-level ``except``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mcp_server.public.domains import public_domains_enabled
from models.public_mcp_domain import MAINTENANCE_INTERVAL_SECONDS
from services.public_mcp_domain_maintenance import run_maintenance
from tasks.job_runs import JobResult, job_failed
from utils.logger import get_logger

logger = get_logger(__name__)

JOB_ID = "public_mcp_domain_maintenance"


async def public_mcp_domain_maintenance_task() -> JobResult:
    """One maintenance run; integer counts, or ``job_failed`` when it raised."""
    if not public_domains_enabled():
        return {"skipped": 1}
    try:
        counts = await run_maintenance()
    except Exception as exc:
        logger.warning("public_mcp_domain_maintenance_failed", error_class=type(exc).__name__)
        return job_failed(exc)
    return counts.as_dict()


def schedule_public_mcp_domain_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the maintenance job, only while the switch is on.

    Args:
        scheduler: The shared APScheduler instance.
    """
    if not public_domains_enabled():
        logger.info("public_mcp_domain_maintenance_disabled")
        return
    scheduler.add_job(
        public_mcp_domain_maintenance_task,
        trigger=IntervalTrigger(seconds=MAINTENANCE_INTERVAL_SECONDS),
        id=JOB_ID,
        name="Public MCP custom domains: checks and housekeeping (#2809)",
        # Also once right at scheduler start (an interval trigger's first fire
        # is one interval after registration): a leader that just took over
        # renews the leases first.
        next_run_time=datetime.now(UTC),
        replace_existing=True,
    )
    logger.info(
        "scheduled_public_mcp_domain_maintenance", interval_seconds=MAINTENANCE_INTERVAL_SECONDS
    )


__all__ = [
    "JOB_ID",
    "public_mcp_domain_maintenance_task",
    "schedule_public_mcp_domain_tasks",
]
