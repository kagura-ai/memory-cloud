"""SQLAlchemy models for Memory Broadlistening analyses.

Issue #494 (umbrella #493): persistence layer for memory clustering
analyses. v1 is flat + snapshot-only — one ``memory_analyses`` row per
run, ``memory_analysis_clusters`` rows for the flat cluster set, and
``memory_analysis_assignments`` rows for the per-memory 2D coordinate +
cluster assignment. ``parent_id`` on clusters is nullable to leave room
for the v2 hierarchical layer without another migration.

Cost rows are NOT in this module — analysis runs emit a
``sleep_reports`` row with ``source='analysis'`` / ``paid_by='byok'``
through the cost-grade plumbing introduced by #523.
"""

import uuid
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Service-layer validation tuples so #495 pipeline can raise ValueError
# instead of catching IntegrityError after the DB round-trip. The DB
# CHECK constraint is the authoritative source — the ``__table_args__``
# CHECK strings below are built from these tuples so the two paths can
# never drift.
MEMORY_ANALYSIS_STATUSES: tuple[str, ...] = (
    "running",
    "succeeded",
    "failed",
    "cancelled",
)
MEMORY_ANALYSIS_PAID_BY_VALUES: tuple[str, ...] = ("byok", "platform")

# Issue #496: ``cancellation_reason`` enum-style values populated when
# a run is soft-cancelled. Free-form for v1 (no DB CHECK), but the
# tuple is the canonical taxonomy so route + MCP code share spelling.
# Future taxonomy may add ``"timeout" / "admin" / "cost_cap"``.
MEMORY_ANALYSIS_CANCELLATION_REASONS: tuple[str, ...] = ("user",)


def _check_in_sql(column: str, values: tuple[str, ...]) -> str:
    """Render a ``column IN ('a', 'b', ...)`` CHECK clause from a tuple."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class MemoryAnalysis(Base):
    """One broadlistening run over a context's memories.

    Attributes:
        id: UUID primary key
        workspace_id: Owning workspace (CASCADE on delete)
        context_id: Target context (CASCADE on delete)
        started_at: Run start time
        finished_at: Run completion time (NULL while running)
        status: running / succeeded / failed / cancelled
        triggered_by: User id that triggered the run
        model_id: FK to ``llm_pricing`` (BIGINT) — RESTRICT on delete
        model_snapshot: Pricing row frozen at run start (JSONB)
        embedding_model: Single embedding model used for this run
        params: Run parameters (filters, query, etc., JSONB)
        input_count: Number of memories included
        cost_estimated_cents: Pre-flight cost estimate
        cost_actual_cents: Actual measured cost
        paid_by: byok / platform (v1 = byok-only)
        quality: Cluster-quality metrics (silhouette, label_confidence...)
        overview: LLM-generated narrative
        error: Failure detail when status='failed'
    """

    __tablename__ = "memory_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Dual default (Python + server) so flush() reads ``running`` without
    # a refresh AND raw INSERT paths satisfy NOT NULL.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="running",
        server_default=text("'running'"),
    )
    triggered_by: Mapped[str] = mapped_column(String(255), nullable=False)

    model_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("llm_pricing.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_count: Mapped[int] = mapped_column(Integer, nullable=False)

    cost_estimated_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_actual_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ``platform`` reserved for v2 platform-paid mode; all v1 runs are BYOK.
    paid_by: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="byok",
        server_default=text("'byok'"),
    )

    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Issue #496: human-readable cancellation reason when status='cancelled'.
    # NULL for non-cancelled runs. Distinct from `error` so a future
    # taxonomy can branch on (status='cancelled', reason='timeout' / 'admin'
    # / 'cost_cap' / 'user') without overloading the failure column.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _check_in_sql("status", MEMORY_ANALYSIS_STATUSES),
            name="valid_memory_analysis_status",
        ),
        CheckConstraint(
            _check_in_sql("paid_by", MEMORY_ANALYSIS_PAID_BY_VALUES),
            name="valid_memory_analysis_paid_by",
        ),
        Index(
            "idx_memory_analyses_workspace_started",
            "workspace_id",
            "started_at",
        ),
        Index(
            "idx_memory_analyses_context_started",
            "context_id",
            "started_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MemoryAnalysis(id={self.id}, context={self.context_id}, "
            f"status={self.status}, started_at={self.started_at})>"
        )


class MemoryAnalysisCluster(Base):
    """One cluster within an analysis run.

    ``parent_id`` is nullable so the v2 hierarchical layer can land
    without another migration. v1 always writes NULL.

    Attributes:
        id: UUID primary key
        analysis_id: Parent run (CASCADE on delete)
        parent_id: Self-reference for v2 hierarchy (NULL on v1)
        cluster_index: Stable ordering within an analysis run
        label: LLM-generated cluster label
        description: Optional longer description
        count: Number of memories in this cluster
        centroid_2d: 2D-projected centroid for the scatter view
        representative_memory_ids: Top-k memory ids that exemplify the cluster
        property_stats: Aggregated facets (JSONB)
        label_confidence: 0..1 score so the UI can gate weak labels
    """

    __tablename__ = "memory_analysis_clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_analysis_clusters.id", ondelete="CASCADE"),
        nullable=True,
    )
    cluster_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    centroid_2d: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
    # NOTE: PostgreSQL cannot enforce FK on array elements, so a memory
    # deleted after a run lands here as a stale UUID. Read paths
    # (#496 API / #497 frontend) MUST LEFT JOIN against ``memories`` and
    # filter out NULLs rather than trusting every ID resolves.
    representative_memory_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
    )
    property_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    label_confidence: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "label_confidence >= 0 AND label_confidence <= 1",
            name="valid_memory_analysis_cluster_label_confidence",
        ),
        Index(
            "idx_memory_analysis_clusters_analysis",
            "analysis_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MemoryAnalysisCluster(id={self.id}, analysis={self.analysis_id}, "
            f"label={self.label!r}, count={self.count})>"
        )


class MemoryAnalysisAssignment(Base):
    """Per-memory 2D coordinate + cluster assignment for an analysis run.

    Composite PK ``(analysis_id, memory_id)`` because the same memory
    can appear in many different runs but exactly once per run.

    Attributes:
        analysis_id: Parent run (CASCADE on delete)
        memory_id: Target memory (CASCADE on delete)
        cluster_id: Cluster the memory was assigned to (CASCADE on delete)
        x: 2D-projected x coordinate
        y: 2D-projected y coordinate
    """

    __tablename__ = "memory_analysis_assignments"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_analyses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_analysis_clusters.id", ondelete="CASCADE"),
        nullable=False,
    )
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index(
            "idx_memory_analysis_assignments_cluster",
            "cluster_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MemoryAnalysisAssignment(analysis={self.analysis_id}, "
            f"memory={self.memory_id}, cluster={self.cluster_id})>"
        )
