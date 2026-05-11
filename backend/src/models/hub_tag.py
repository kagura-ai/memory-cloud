"""SQLAlchemy model for tag co-occurrence hub-tag cache.

Issue #223: per-(workspace, context) snapshot of tags considered "hub"
(frequency above ``tag_cooccurrence_hub_threshold``). Refreshed nightly by
Sleep Maintenance and read by ``_create_tag_cooccurrence_seed_edges`` at
remember() time to prune candidate matches that share only popular tags.

Design:
    - One row per (workspace, context); refresh = upsert on
      ``uq_hub_tag_cache_ws_ctx``.
    - ``hub_tags`` is a JSONB list of tag strings. Empty list ([]) is a
      valid "no hub tags this run" result; missing row is treated as
      "no exclusion" by readers (graceful first-night behavior).
    - Mirrors the ``Bm25IdfDriftLog`` (#343) precedent for per-context
      computed state.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class HubTagCache(Base):
    """Per-(workspace, context) cache of hub tags for tag_cooccurrence seeding.

    Attributes:
        id: Auto-increment primary key
        workspace_id: Workspace UUID (denormalized; not FK because the
            workspace registry lives outside this app's owned tables)
        context_id: Context UUID (FK with CASCADE on context delete)
        hub_tags: JSONB list of tag strings considered hub for this scope.
            Empty list = "computed and found nothing", missing row = "never
            computed"
        memory_count: Total memory count at compute time. Useful for
            debugging "why is X considered hub?" without re-querying memories
        threshold_used: ``tag_cooccurrence_hub_threshold`` value applied at
            compute time (so historical inspection of a row still makes sense
            if the configured threshold changes)
        computed_at: Cron cycle timestamp (tz-aware)
    """

    __tablename__ = "hub_tag_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    hub_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    memory_count: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_used: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "context_id", name="uq_hub_tag_cache_ws_ctx"),
        CheckConstraint("memory_count >= 0", name="hub_tag_cache_nonneg_memory_count"),
        CheckConstraint(
            "threshold_used >= 0.0 AND threshold_used <= 1.0",
            name="hub_tag_cache_threshold_in_range",
        ),
        Index("ix_hub_tag_cache_workspace_id", "workspace_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<HubTagCache(workspace_id={self.workspace_id}, "
            f"context_id={self.context_id}, hub_tags={self.hub_tags})>"
        )
