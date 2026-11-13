"""The operations status document (Issue #2768): jobs, backlogs, LLM lanes.

:func:`read_operations_status` assembles what ``GET
/api/v1/admin/operations/status`` returns from stored rows only, so any API
process answers (the leader writes the rows; the answering process needs no
scheduler and no LLM credential).

The request is bounded: its transaction is read-only and every statement has
a timeout; the whole read has a wall-clock budget, after which the remaining
backlog and lane entries carry ``error_class: "DeadlineExceeded"``.

Aggregates only: code- or deployment-defined names, times, integers and
exception CLASS names. No user, workspace or context identifier, no address,
no content, no exception message.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field
from sqlalchemy import select, text

from models.api_base import TZAwareBaseModel
from models.operations_status import OperationsLlmLane, ScheduledJobStatus
from services.operations.backlogs import DEADLINE_EXCEEDED, read_backlogs
from services.operations.job_status import is_overdue, is_running
from services.operations.llm_lanes import UsageAttribution, lane_usage
from tasks.job_runs import sanitize_counts, sanitize_error_class

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.settings import Settings

SCHEMA_VERSION = 1
REQUEST_BUDGET_SECONDS = 10.0
STATEMENT_TIMEOUT_MS = 5000
PROBE_JOB_ID = "operations_probe"


class JobStatusV1(TZAwareBaseModel):
    """One scheduled job (one row per job id the leader ever registered)."""

    job_id: str
    name: str
    trigger: Literal["interval", "cron", "other"]
    expected_interval_seconds: int | None = Field(
        description="Longest gap between two fires; null = never overdue."
    )
    registered: bool = Field(
        description="Part of the leader's latest registration; false = no longer scheduled."
    )
    running: bool = Field(description="A run was submitted after the latest outcome.")
    overdue: bool = Field(
        description="Registered, and no run started within the interval plus the grace."
    )
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_outcome: Literal["ok", "failed", "missed"] | None
    last_outcome_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    error_class: str | None = Field(
        description="Exception class name of the latest failed run (never the message)."
    )
    last_duration_ms: int | None
    counts: dict[str, int] | None = Field(
        description="Integer counts the latest finished run returned."
    )


class BacklogStatusV1(TZAwareBaseModel):
    """One work queue: its size and its oldest item."""

    name: str
    drained_by: list[str] = Field(description="Job ids that drain it; empty = people do.")
    count: int | None
    oldest_at: datetime | None
    oldest_age_seconds: float | None
    error_class: str | None = Field(
        description="Set when the count could not be read (DeadlineExceeded = out of time)."
    )


class LaneProbeV1(TZAwareBaseModel):
    """The latest opt-in probe of a lane."""

    last_probe_at: datetime
    last_outcome: Literal["ok", "failed"]
    last_latency_ms: int | None
    error_class: str | None
    consecutive_failures: int
    last_success_at: datetime | None


class LlmLaneV1(TZAwareBaseModel):
    """One platform LLM lane as the scheduler leader configured it."""

    lane: str
    provider: str
    model: str
    credential_present: bool = Field(
        description="The leader holds the lane's platform credential (never the credential)."
    )
    snapshot_at: datetime
    usage_recorded: bool = Field(description="Whether the lane's calls are ledgered at all.")
    usage_attribution: UsageAttribution = Field(
        description=(
            "platform, or includes_workspace_keys when the ledger cannot tell "
            "platform-paid calls from calls on a workspace's own key."
        )
    )
    last_success_at: datetime | None
    calls_last_hour: int | None
    calls_last_day: int | None
    error_class: str | None = Field(
        description="Set when the usage could not be read (DeadlineExceeded = out of time)."
    )
    probe: LaneProbeV1 | None


class OperationsStatusV1(TZAwareBaseModel):
    """The operations status document (aggregates only)."""

    schema_version: Literal[1] = SCHEMA_VERSION
    as_of: datetime
    overdue_grace_seconds: int
    scheduler_registered_at: datetime | None = Field(
        description="When the current leader registered its jobs (null = never)."
    )
    probe_enabled: bool = Field(description="The opt-in LLM lane probe is registered.")
    jobs: list[JobStatusV1]
    backlogs: list[BacklogStatusV1]
    llm: list[LlmLaneV1]


@dataclass(frozen=True)
class _LaneRow:
    lane: str
    provider: str
    model: str
    credential_present: bool
    snapshot_at: datetime
    probe: LaneProbeV1 | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _job(
    row: ScheduledJobStatus, *, now: datetime, grace: int, latest: datetime | None
) -> JobStatusV1:
    trigger: Any = row.trigger_kind if row.trigger_kind in ("interval", "cron") else "other"
    outcome: Any = row.last_outcome if row.last_outcome in ("ok", "failed", "missed") else None
    return JobStatusV1(
        job_id=row.job_id,
        name=row.job_name,
        trigger=trigger,
        expected_interval_seconds=row.expected_interval_seconds,
        registered=latest is not None and row.last_registered_at == latest,
        running=is_running(row),
        overdue=is_overdue(row, now=now, grace_seconds=grace, latest_registration=latest),
        last_started_at=_aware(row.last_started_at),
        last_finished_at=_aware(row.last_finished_at),
        last_outcome=outcome,
        last_outcome_at=_aware(row.last_outcome_at),
        last_success_at=_aware(row.last_success_at),
        consecutive_failures=row.consecutive_failures,
        error_class=(sanitize_error_class(row.last_error_class) if row.last_error_class else None),
        last_duration_ms=row.last_duration_ms,
        # Sanitised again on the way out: the stored value is published.
        counts=sanitize_counts(row.last_counts),
    )


def _probe(row: OperationsLlmLane) -> LaneProbeV1 | None:
    if row.last_probe_at is None or row.last_probe_outcome not in ("ok", "failed"):
        return None
    outcome: Any = row.last_probe_outcome
    return LaneProbeV1(
        last_probe_at=row.last_probe_at,
        last_outcome=outcome,
        last_latency_ms=row.last_probe_latency_ms,
        error_class=(
            sanitize_error_class(row.last_probe_error_class) if row.last_probe_error_class else None
        ),
        consecutive_failures=row.probe_consecutive_failures,
        last_success_at=_aware(row.last_probe_success_at),
    )


async def _set_statement_timeout(db: AsyncSession, left: float) -> None:
    ms = max(1, min(STATEMENT_TIMEOUT_MS, int(left * 1000)))
    await db.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(ms)})


async def read_operations_status(
    db: AsyncSession,
    *,
    now: datetime,
    settings: Settings,
    budget_seconds: float = REQUEST_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> OperationsStatusV1:
    """Assemble the status document from the stored rows.

    Must run first in ``db``'s transaction: it makes the transaction
    read-only (the route writes nothing).

    Args:
        db: The request's session (no statement executed yet).
        now: Timezone-aware current time.
        settings: The answering process's settings (the overdue grace and
            ``RESOLVE_STORED_BYOK_KEYS``; no LLM setting is read).
        budget_seconds: Wall-clock budget of the whole read.
        clock: Monotonic clock (tests pass a fake).
    """
    deadline_at = clock() + budget_seconds

    def left() -> float:
        return deadline_at - clock()

    await db.execute(text("SET TRANSACTION READ ONLY"))
    await _set_statement_timeout(db, left())

    grace = settings.operations_status_overdue_grace_seconds
    rows = list(
        (await db.execute(select(ScheduledJobStatus).order_by(ScheduledJobStatus.job_id)))
        .scalars()
        .all()
    )
    latest = max((r.last_registered_at for r in rows), default=None)
    jobs = [_job(r, now=now, grace=grace, latest=latest) for r in rows]
    probe_enabled = any(j.job_id == PROBE_JOB_ID and j.registered for j in jobs)

    # Plain values before any savepoint: a rolled-back savepoint may expire ORM rows.
    lane_rows = [
        _LaneRow(
            lane=row.lane,
            provider=row.provider,
            model=row.model,
            credential_present=row.credential_present,
            snapshot_at=row.snapshot_at,
            probe=_probe(row),
        )
        for row in (
            await db.execute(select(OperationsLlmLane).order_by(OperationsLlmLane.lane))
        ).scalars()
    ]

    readings = await read_backlogs(db, now, settings, deadline=left)
    backlogs = [
        BacklogStatusV1(
            name=r.name,
            drained_by=list(r.drained_by),
            count=r.count,
            oldest_at=r.oldest_at,
            oldest_age_seconds=(
                max(0.0, (now - r.oldest_at).total_seconds()) if r.oldest_at is not None else None
            ),
            error_class=r.error_class,
        )
        for r in readings
    ]

    llm: list[LlmLaneV1] = []
    for row in lane_rows:
        error_class: str | None = None
        usage = None
        if left() <= 0:
            error_class = DEADLINE_EXCEEDED
        else:
            try:
                async with db.begin_nested():
                    await _set_statement_timeout(db, left())
                    usage = await lane_usage(
                        db,
                        row.lane,
                        row.provider,
                        row.model,
                        now=now,
                        resolve_stored_byok_keys=settings.resolve_stored_byok_keys,
                    )
            except Exception as exc:  # noqa: BLE001 - one lane's failure never hides the rest
                error_class = sanitize_error_class(type(exc).__name__)
        llm.append(
            LlmLaneV1(
                lane=row.lane,
                provider=row.provider,
                model=row.model,
                credential_present=row.credential_present,
                snapshot_at=row.snapshot_at,
                usage_recorded=usage.recorded if usage else row.lane != "rerank",
                usage_attribution=(
                    usage.attribution
                    if usage
                    else (
                        "includes_workspace_keys"
                        if row.lane == "sleep_judge" and settings.resolve_stored_byok_keys
                        else "platform"
                    )
                ),
                last_success_at=usage.last_success_at if usage else None,
                calls_last_hour=usage.calls_last_hour if usage else None,
                calls_last_day=usage.calls_last_day if usage else None,
                error_class=error_class,
                probe=row.probe,
            )
        )

    return OperationsStatusV1(
        as_of=now,
        overdue_grace_seconds=grace,
        scheduler_registered_at=_aware(latest),
        probe_enabled=probe_enabled,
        jobs=jobs,
        backlogs=backlogs,
        llm=llm,
    )


__all__ = [
    "REQUEST_BUDGET_SECONDS",
    "SCHEMA_VERSION",
    "BacklogStatusV1",
    "JobStatusV1",
    "LaneProbeV1",
    "LlmLaneV1",
    "OperationsStatusV1",
    "read_operations_status",
]
