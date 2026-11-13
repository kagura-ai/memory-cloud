"""Operations status jobs of the scheduler leader (Issue #2768).

- ``operations_lane_snapshot`` (always on, every 5 minutes and once right at
  scheduler start): writes the platform LLM lane set to
  ``operations_llm_lanes`` so any API process can answer the status route
  without LLM settings or credentials. No external call. Its run also
  serves as a heartbeat row among the jobs.
- ``operations_probe`` (opt-in, ``OPERATIONS_PROBE_INTERVAL_SECONDS`` > 0,
  at least 300): one minimal paid call per distinct platform lane target
  (:mod:`services.operations.probe`), within a run budget of
  ``min(interval / 2, 120 s)``.

Both return integer counts (published to operators by the status route) and
``job_failed(exc)`` from their top-level ``except``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import get_settings
from tasks.job_runs import JobResult, job_failed
from utils.logger import get_logger

logger = get_logger(__name__)

LANE_SNAPSHOT_JOB_ID = "operations_lane_snapshot"
PROBE_JOB_ID = "operations_probe"
LANE_SNAPSHOT_INTERVAL_SECONDS = 300


async def operations_lane_snapshot_task() -> JobResult:
    """Write the platform lane set (no external call)."""
    from services.operations.llm_lanes import read_platform_lanes, write_lane_snapshot

    try:
        lanes = await read_platform_lanes(get_settings())
        await write_lane_snapshot(lanes)
    except Exception as exc:
        logger.warning("operations_lane_snapshot_failed", error_class=type(exc).__name__)
        return job_failed(exc)
    return {
        "lanes": len(lanes),
        "credentials_missing": sum(1 for lane in lanes if not lane.credential_present),
    }


async def operations_probe_task() -> JobResult:
    """Probe every distinct platform lane target once, within the run budget."""
    from services.operations.llm_lanes import read_platform_lanes, write_lane_snapshot
    from services.operations.probe import (
        PROBE_TIMEOUT_SECONDS,
        ProbeClients,
        ledger_probe,
        probe_targets,
        record_probe,
        run_budget_seconds,
        run_probe,
    )

    settings = get_settings()
    counts = {"targets": 0, "failed": 0, "budget_skipped": 0, "record_failed": 0}
    try:
        lanes = await read_platform_lanes(settings)
        # The probe updates the lane rows: make sure they describe this config.
        await write_lane_snapshot(lanes)
        targets = probe_targets(lanes)
        counts["targets"] = len(targets)
        deadline = time.monotonic() + run_budget_seconds(settings.operations_probe_interval_seconds)
        clients = ProbeClients(settings)
        for target in targets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                counts["budget_skipped"] += 1
                continue
            result = await run_probe(
                target, clients, settings, timeout=min(PROBE_TIMEOUT_SECONDS, remaining)
            )
            if not result.ok:
                counts["failed"] += 1
            try:
                await record_probe(result)
            except Exception as exc:  # noqa: BLE001 - the next target still runs
                counts["record_failed"] += 1
                logger.warning("operations_probe_record_failed", error_class=type(exc).__name__)
            await ledger_probe(result)
    except Exception as exc:
        logger.warning("operations_probe_failed", error_class=type(exc).__name__)
        return job_failed(exc)
    if counts["failed"]:
        logger.info("operations_probe_lanes_failed", **counts)
    return counts


def schedule_operations_probe_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the lane snapshot (always) and the probe (when its interval is set)."""
    scheduler.add_job(
        operations_lane_snapshot_task,
        trigger=IntervalTrigger(seconds=LANE_SNAPSHOT_INTERVAL_SECONDS),
        id=LANE_SNAPSHOT_JOB_ID,
        name="Operations status: LLM lane snapshot (Issue #2768)",
        # Also once right at scheduler start (an interval trigger's first fire
        # is one interval after registration).
        next_run_time=datetime.now(UTC),
        replace_existing=True,
    )
    interval = get_settings().operations_probe_interval_seconds
    if interval <= 0:
        logger.info("operations_probe_disabled")
        return
    scheduler.add_job(
        operations_probe_task,
        trigger=IntervalTrigger(seconds=interval),
        id=PROBE_JOB_ID,
        name="Operations status: LLM lane probe (Issue #2768)",
        replace_existing=True,
    )
    logger.info("scheduled_operations_probe", interval_seconds=interval)


__all__ = [
    "LANE_SNAPSHOT_JOB_ID",
    "PROBE_JOB_ID",
    "operations_lane_snapshot_task",
    "operations_probe_task",
    "schedule_operations_probe_tasks",
]
