"""Durable UUID-only erasure scopes; independent of cascading memory rows."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class VectorErasureJob(Base):
    __tablename__ = "vector_erasure_jobs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("erasure_requests.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    point_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    subject_pseudonym: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    captured_before_erasure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("request_id", "memory_id", "point_id", name="uq_vector_erasure_scope"),
        Index("ix_vector_erasure_pending", "completed_at", "id"),
    )
