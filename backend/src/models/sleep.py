"""SQLAlchemy models for Sleep Maintenance.

Issue #101: Sleep Maintenance Foundation — report tracking and audit log.
"""

from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from db.base import Base


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
        status: running / completed / failed / cancelled
        edge_discovery_result: Phase 1 results (JSON)
        dedup_result: Phase 2 results (JSON)
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
    """

    __tablename__ = "sleep_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(String(255), nullable=False, index=True)
    workspace_id = Column(UUID(as_uuid=True), nullable=True)
    context_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Execution timing
    started_at = Column(DateTime, nullable=False, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running")

    # Per-phase results (JSON)
    edge_discovery_result = Column(JSON, nullable=True)
    dedup_result = Column(JSON, nullable=True)
    importance_result = Column(JSON, nullable=True)
    consolidation_result = Column(JSON, nullable=True)
    reindex_result = Column(JSON, nullable=True)

    # Cost tracking
    llm_calls_made = Column(Integer, nullable=False, default=0)
    llm_tokens_used = Column(Integer, nullable=False, default=0)
    embedding_calls_made = Column(Integer, nullable=False, default=0)

    # Activity counters
    memories_processed = Column(Integer, nullable=False, default=0)
    edges_created = Column(Integer, nullable=False, default=0)
    memories_merged = Column(Integer, nullable=False, default=0)
    memories_promoted = Column(Integer, nullable=False, default=0)
    memories_flagged = Column(Integer, nullable=False, default=0)

    # Error tracking
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="valid_sleep_report_status",
        ),
        Index("idx_sleep_reports_user_status", "user_id", "status"),
        Index("idx_sleep_reports_started_at", "started_at"),
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

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    report_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sleep_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    phase = Column(String(30), nullable=False)
    action_type = Column(String(30), nullable=False)
    memory_id = Column(UUID(as_uuid=True), nullable=True)
    target_id = Column(UUID(as_uuid=True), nullable=True)
    details = Column(JSON, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_sleep_actions_report_phase", "report_id", "phase"),)

    def __repr__(self) -> str:
        return f"<SleepAction(id={self.id}, phase={self.phase}, action={self.action_type})>"
