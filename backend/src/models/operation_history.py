"""Workspace mutation metadata, retained independently of the target (#2739)."""

from datetime import datetime
from uuid import UUID as UUIDType

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.json_types import JSONB


class OperationHistory(Base):
    """No content, credentials, free-form metadata, or cascading foreign keys."""

    __tablename__ = "workspace_operation_history"

    id: Mapped[UUIDType] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUIDType] = mapped_column(UUID(as_uuid=True), nullable=False)
    context_id: Mapped[UUIDType | None] = mapped_column(UUID(as_uuid=True))
    context_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context_owner_id: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(255))
    subject_user_id: Mapped[str | None] = mapped_column(String(255))
    agent_id: Mapped[UUIDType | None] = mapped_column(UUID(as_uuid=True))
    surface: Mapped[str] = mapped_column(String(10), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint("count > 0", name="operation_history_positive_count"),
        CheckConstraint(
            "outcome IN ('success', 'failure', 'denied')", name="operation_history_outcome"
        ),
        CheckConstraint(
            "surface IN ('web', 'api', 'mcp', 'system', 'unknown')",
            name="operation_history_surface",
        ),
        Index("idx_operation_history_workspace_time", "workspace_id", "occurred_at", "id"),
        Index("idx_operation_history_context", "workspace_id", "context_id", "occurred_at"),
        Index("idx_operation_history_actor", "actor_user_id"),
        Index("idx_operation_history_subject", "subject_user_id"),
        Index("idx_operation_history_context_owner", "context_owner_id"),
    )
