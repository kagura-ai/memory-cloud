"""Vector point deletes that ``forget`` could not run after its commit (#2476).

``forget`` queues one row per forgotten point in the transaction that
tombstones the memory, deletes the point after the commit when no point writer
is mid-transaction, and removes the row. A row that stays is retried by the
drain job (``services.deferred_point_deletes``). UUIDs and a collection name
only — no content, author or workspace.

No foreign key to ``memories``: the tombstone purge and erasure hard-delete
rows, and a cascade would drop exactly the retries nothing else would run.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DeferredPointDelete(Base):
    __tablename__ = "deferred_point_deletes"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    memory_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    point_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    collection: Mapped[str] = mapped_column(String(255), nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        UniqueConstraint("memory_id", "point_id", "collection", name="uq_deferred_point_delete"),
        # The drain reads fewest attempts first, then oldest.
        Index("ix_deferred_point_deletes_queued", "attempts", "queued_at", "id"),
    )
