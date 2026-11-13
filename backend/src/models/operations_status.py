"""Operations status tables (Issue #2768).

Two small tables the scheduler leader writes and any API process reads, so an
operator can see a job that stopped or keeps failing without the container
logs. Both hold aggregates only: code- or deployment-defined names, times,
integers and exception CLASS names, never a user, workspace or context
identifier, an exception message or a traceback.

- ``scheduled_job_status``: one row per scheduled job id, updated in place
  (not a history). Written by the run recorder (``tasks/job_runs.py``).
- ``operations_llm_lanes``: one row per platform LLM lane (provider, model,
  whether the leader holds a platform credential, and the last opt-in probe).

Neither table has a foreign key or a customer column; both are tiny (about 25
and at most 6 rows), so neither needs a secondary index.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.json_types import NullableJSONB

# Byte-identical to the CHECKs below and to the f39 migration (schema drift test).
JobTriggerKind = Literal["interval", "cron", "other"]
JOB_TRIGGER_KINDS: tuple[JobTriggerKind, ...] = ("interval", "cron", "other")

JobOutcome = Literal["ok", "failed", "missed"]
JOB_OUTCOMES: tuple[JobOutcome, ...] = ("ok", "failed", "missed")

LlmLane = Literal[
    "embedding", "sleep_judge", "supersede_verify", "recall_selector", "analysis", "rerank"
]
LLM_LANES: tuple[LlmLane, ...] = (
    "embedding",
    "sleep_judge",
    "supersede_verify",
    "recall_selector",
    "analysis",
    "rerank",
)

ProbeOutcome = Literal["ok", "failed"]

# Serialized size cap of ``last_counts`` (the recorder trims to fit).
JOB_COUNTS_MAX_BYTES = 2048

# Column widths the writers clip to.
JOB_ID_MAX_LENGTH = 100
JOB_NAME_MAX_LENGTH = 200
ERROR_CLASS_MAX_LENGTH = 100
LANE_PROVIDER_MAX_LENGTH = 50
LANE_MODEL_MAX_LENGTH = 100


class ScheduledJobStatus(Base):
    """The latest state of one scheduled job.

    Attributes:
        job_id: The APScheduler job id (code-defined snake_case).
        job_name: The job's display name (code-defined).
        trigger_kind: ``interval``, ``cron`` or ``other``.
        expected_interval_seconds: The longest gap between two fires the
            trigger allows; NULL when it cannot be derived (never overdue).
        first_registered_at: When a leader first registered the job.
        last_registered_at: The latest registration generation that included
            the job (one database ``now()`` per registration statement). Run
            writes never touch it.
        registered_since: When the job's current, uninterrupted registration
            began: set on insert and when a registration finds the row absent
            from the previous generation (a re-enabled job).
        last_started_at: The latest submission of a run (wall clock of the
            leader).
        last_finished_at: When the latest run that completed or failed ended.
        last_outcome: ``ok``, ``failed`` or ``missed``.
        last_outcome_at: When ``last_outcome`` was observed.
        last_success_at: The latest ``ok`` run that did real work (a run that
            only reports ``skipped`` does not count).
        consecutive_failures: ``failed`` runs since the last success.
        last_error_class: Exception class name of the latest failure.
        last_duration_ms: Duration of the latest completed or failed run.
        last_counts: Integer counts the latest finished run returned.
        updated_at: Last write.
    """

    __tablename__ = "scheduled_job_status"

    job_id: Mapped[str] = mapped_column(String(JOB_ID_MAX_LENGTH), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(JOB_NAME_MAX_LENGTH), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    expected_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error_class: Mapped[str | None] = mapped_column(
        String(ERROR_CLASS_MAX_LENGTH), nullable=True
    )
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_counts: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('interval', 'cron', 'other')",
            name="valid_scheduled_job_trigger_kind",
        ),
        CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN ('ok', 'failed', 'missed')",
            name="valid_scheduled_job_outcome",
        ),
        CheckConstraint(
            "expected_interval_seconds IS NULL OR expected_interval_seconds > 0",
            name="valid_scheduled_job_interval",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="valid_scheduled_job_failures",
        ),
        CheckConstraint(
            "last_duration_ms IS NULL OR last_duration_ms >= 0",
            name="valid_scheduled_job_duration",
        ),
        CheckConstraint(
            f"last_counts IS NULL OR octet_length(last_counts::text) <= {JOB_COUNTS_MAX_BYTES}",
            name="valid_scheduled_job_counts_size",
        ),
    )

    def __repr__(self) -> str:
        return f"<ScheduledJobStatus(job_id='{self.job_id}', last_outcome='{self.last_outcome}')>"


class OperationsLlmLane(Base):
    """One platform LLM lane as the scheduler leader sees it.

    The leader writes the lane set (no external call) when its scheduler starts
    and on a short interval; the opt-in probe fills the probe columns. The
    answering API process never reads LLM settings or credentials for it.

    Attributes:
        lane: One of the six lane names.
        provider: Provider name of the lane's platform configuration.
        model: Model name of the lane's platform configuration.
        credential_present: Whether the leader holds a platform credential for
            the lane (never the credential itself).
        snapshot_at: When the leader last wrote the lane's configuration.
        last_probe_at: When the probe last called the lane.
        last_probe_outcome: ``ok`` or ``failed``.
        last_probe_latency_ms: Latency of the latest probe call.
        last_probe_error_class: Exception class name of the latest failed probe.
        probe_consecutive_failures: Failed probes since the last good one.
        last_probe_success_at: The latest ``ok`` probe.
        updated_at: Last write.
    """

    __tablename__ = "operations_llm_lanes"

    lane: Mapped[str] = mapped_column(String(30), primary_key=True)
    provider: Mapped[str] = mapped_column(String(LANE_PROVIDER_MAX_LENGTH), nullable=False)
    model: Mapped[str] = mapped_column(String(LANE_MODEL_MAX_LENGTH), nullable=False)
    credential_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_probe_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_probe_error_class: Mapped[str | None] = mapped_column(
        String(ERROR_CLASS_MAX_LENGTH), nullable=True
    )
    probe_consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_probe_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "lane IN ('embedding', 'sleep_judge', 'supersede_verify', 'recall_selector', "
            "'analysis', 'rerank')",
            name="valid_operations_llm_lane",
        ),
        CheckConstraint(
            "last_probe_outcome IS NULL OR last_probe_outcome IN ('ok', 'failed')",
            name="valid_operations_llm_lane_probe_outcome",
        ),
        CheckConstraint(
            "probe_consecutive_failures >= 0",
            name="valid_operations_llm_lane_probe_failures",
        ),
        CheckConstraint(
            "last_probe_latency_ms IS NULL OR last_probe_latency_ms >= 0",
            name="valid_operations_llm_lane_probe_latency",
        ),
    )

    def __repr__(self) -> str:
        return f"<OperationsLlmLane(lane='{self.lane}', provider='{self.provider}')>"
