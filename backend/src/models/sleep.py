"""SQLAlchemy models for Sleep Maintenance.

Issue #101: Sleep Maintenance Foundation — report tracking and audit log.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Allowed values for the sleep_reports cost-grade dimensions (#523).
# Mirror the ``LLM_PRICING_UNIT_TYPES`` pattern: the DB CHECK constraint is
# the source of truth, these tuples exist for service-layer validation and
# are imported by future call sites (#495 Memory Analysis pipeline) to
# validate inputs cleanly with ValueError rather than IntegrityError.
# Keep in sync with the ``valid_sleep_report_source`` /
# ``valid_sleep_report_paid_by`` CHECK strings in ``SleepReport.__table_args__``
# and the matching ``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`` strings in
# the migration (raw ``op.execute(sa.text(...))`` form, see d05_523).
SLEEP_REPORT_SOURCES: tuple[str, ...] = ("sleep", "analysis")
SLEEP_REPORT_PAID_BY_VALUES: tuple[str, ...] = ("platform", "byok")

# Issue #504: per-context sleep_mode is the source of truth for which Sleep
# Maintenance phases run. Mirrors ``Context.sleep_mode`` column values and the
# branches in ``services.sleep.orchestrator.SleepOrchestrator.run``.
SleepMode = Literal["full", "edges_only", "skip"]


class SleepReport(Base):
    """Sleep Maintenance execution report.

    Tracks each sleep maintenance run with per-phase results,
    LLM usage statistics, and overall execution status.

    Attributes:
        id: UUID primary key
        user_id: Target user
        workspace_id: Target workspace
        context_id: Target context
        started_at: Execution start time
        completed_at: Execution end time
        status: running / completed / degraded / failed / cancelled / rolled_back
            (#1183: 'degraded' = finished with partial judge-LLM failures;
            total judge failure is graded 'failed')
        edge_discovery_result: Phase 1 results (JSON)
        dedup_result: Phase 2 results (JSON)
        merge_retention_result: Phase 2.5 results (JSON, #1209)
        importance_result: Phase 3 results (JSON)
        consolidation_result: Phase 4 results (JSON)
        reindex_result: Phase 5 results (JSON)
        llm_calls_made: Total LLM API calls
        llm_tokens_used: Total tokens consumed
        embedding_calls_made: Total embedding API calls
        memories_processed: Total memories touched
        edges_created: New edges from Edge Discovery
        memories_merged: Memories merged in Dedup
        memories_promoted: Working -> Persistent promotions
        memories_flagged: Memories flagged for review
        error_message: Error details if failed
        llm_call_failures: Judge-LLM calls that raised, across all phases (#1183)
    """

    __tablename__ = "sleep_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Execution timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="running",
        server_default="running",
    )

    # Per-phase results (JSON)
    edge_discovery_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    dedup_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # #1209: merge_retention phase (purge window) results.
    merge_retention_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    importance_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    consolidation_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reindex_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Cost tracking — legacy roll-up totals (#101).
    # Issue #471 split per-(phase, provider, model) LLM usage into the
    # ``sleep_report_llm_usage`` child table (see below). These three columns
    # remain populated by the reporter as a sum of child rows for back-compat:
    # any reader that selects them directly (legacy dashboards, log analyzers,
    # MCP tools) continues to work without change.
    llm_calls_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    llm_tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    embedding_calls_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Embedding cost-grade columns (#471). Embedding is instance-global per
    # ``backend/src/config/settings.py`` (one EMBEDDING_PROVIDER /
    # EMBEDDING_MODEL per process), so a single (provider, model, tokens)
    # triple per run is sufficient — no child table needed.
    #
    # NOTE for v0.15.0: these columns track ONLY the reindex phase's embedding
    # API calls today, mirroring the existing ``embedding_calls_made``
    # behavior. Sleep phases 1 (edge_discovery) and 2 (dedup_merge) also call
    # the embedding API but don't increment any counter; #475 closes that gap.
    embedding_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Cost-grade dimensions (#523). Both columns carry a Python-side
    # ``default`` (in-memory ORM objects readable after flush without
    # refresh) AND a ``server_default`` (raw INSERT paths that bypass the
    # ORM still satisfy NOT NULL). #472 aggregates by these axes.
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="sleep",
        server_default=text("'sleep'"),
    )
    paid_by: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="platform",
        server_default=text("'platform'"),
    )

    # Activity counters
    memories_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    edges_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memories_merged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memories_promoted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memories_flagged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # #1183: judge-LLM calls that raised across all phases. Backs the
    # degraded/failed status grading in ``SleepReporter.complete_report`` and
    # lets dashboards aggregate judge health without parsing per-phase JSON.
    llm_call_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        # 'degraded' (#1183): run finished but SOME judge-LLM calls failed.
        # Total judge failure grades as 'failed' (see reporter.complete_report).
        CheckConstraint(
            "status IN ('running', 'completed', 'degraded', 'failed', 'cancelled', 'rolled_back')",
            name="valid_sleep_report_status",
        ),
        # #523 cost-grade dimensions
        CheckConstraint(
            "source IN ('sleep', 'analysis')",
            name="valid_sleep_report_source",
        ),
        CheckConstraint(
            "paid_by IN ('platform', 'byok')",
            name="valid_sleep_report_paid_by",
        ),
        Index("idx_sleep_reports_user_status", "user_id", "status"),
        Index("idx_sleep_reports_started_at", "started_at"),
        # #523 supports #472 aggregation queries that filter by workspace+source
        # and order newest-first. ``text("started_at DESC")`` mirrors the
        # migration so alembic autogenerate sees identical AST nodes.
        Index(
            "idx_sleep_reports_workspace_source_started",
            "workspace_id",
            "source",
            text("started_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SleepReport(id={self.id}, user={self.user_id}, "
            f"status={self.status}, started_at={self.started_at})>"
        )


class SleepAction(Base):
    """Sleep Maintenance audit log entry.

    Records individual actions taken during sleep maintenance
    for auditability and debugging.

    Attributes:
        id: Auto-increment primary key
        report_id: Parent sleep report
        phase: Phase name (edge_discovery, dedup_merge, etc.)
        action_type: Action taken (merge, create_edge, promote, flag, etc.)
        memory_id: Primary memory affected
        target_id: Secondary memory (e.g., merge target)
        details: Additional action details (JSON)
        created_at: Action timestamp
    """

    __tablename__ = "sleep_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sleep_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_sleep_actions_report_phase", "report_id", "phase"),)

    def __repr__(self) -> str:
        return f"<SleepAction(id={self.id}, phase={self.phase}, action={self.action_type})>"


class SleepReportLLMUsage(Base):
    """Per-(phase, provider, model) LLM call breakdown for a sleep run.

    Issue #471: cost-grade telemetry. Captures the dimensions needed to
    compute actual `$` cost from token counts:

    - ``provider`` + ``model`` → joined against ``llm_pricing`` to resolve
      the per-token rate active at the parent run's ``started_at``.
    - ``input_tokens`` / ``output_tokens`` / ``cached_input_tokens`` →
      separate counters because output is typically ~5× input rate and
      Anthropic's prompt cache discounts cached input by ~90%.
    - ``tokenizer_version`` is **audit only**; it is not a price-lookup key
      and the system never re-prices on a tokenizer change. It exists so
      analysts can spot the case where a provider ships a new tokenizer
      under an unchanged-rate model (Anthropic Opus 4.7 issued ~35% more
      tokens than 4.6 for the same text).

    A run typically emits one row per phase × model. Today all phases use
    a single ``config.sleep_llm_model``, so a typical run produces 4-5 rows
    (one per LLM-using phase). The schema is per-(phase, provider, model)
    to remain correct if a future change lets phases pick different models.

    Phases that don't call the LLM (currently only ``reindex``) emit no
    row. The aggregation API in #472 uses these rows directly via
    ``GROUP BY provider, model`` joins; the legacy roll-up columns on
    ``sleep_reports`` are computed as the sum of these rows for back-compat.
    """

    __tablename__ = "sleep_report_llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # report_id has an explicit ``Index`` in __table_args__ below to match
    # the migration's ``idx_sleep_report_llm_usage_report_id`` name.
    # ``index=True`` here would create an auto-named index
    # (``ix_sleep_report_llm_usage_report_id``) and cause schema-drift
    # noise on alembic autogenerate or duplicate indexes when
    # Base.metadata.create_all is used in tests.
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sleep_reports.id", ondelete="CASCADE"),
        nullable=False,
    )

    phase: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cached_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    tokenizer_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Single-column index on report_id (FK join key). PostgreSQL does
        # NOT auto-index FK columns, and the explicit name here matches
        # the migration so model + migration agree on a single index.
        Index(
            "idx_sleep_report_llm_usage_report_id",
            "report_id",
        ),
        # Composite index supports the #472 aggregation queries that
        # ``GROUP BY provider, model`` after filtering by report period.
        Index(
            "idx_sleep_report_llm_usage_provider_model",
            "provider",
            "model",
            "report_id",
        ),
        # CHECK on phase keeps the audit trail readable even if a future
        # phase name slips in via reporter changes — same defensive pattern
        # as ``valid_sleep_report_status`` on ``sleep_reports``.
        # ``cluster_labeling`` was added by the d07_495 migration for the
        # Memory Analysis pipeline (#495); the model CHECK must list the
        # SAME values as the migration, otherwise tests using
        # ``Base.metadata.create_all`` (rather than alembic) build a table
        # with the stricter old CHECK and analysis inserts fail.
        CheckConstraint(
            "phase IN ('edge_discovery', 'dedup_merge', 'importance_reeval', "
            "'consolidation', 'reindex', 'cluster_labeling')",
            name="valid_sleep_report_llm_usage_phase",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SleepReportLLMUsage(report={self.report_id}, "
            f"phase={self.phase}, model={self.model}, "
            f"in={self.input_tokens}, out={self.output_tokens})>"
        )
