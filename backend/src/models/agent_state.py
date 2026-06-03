"""Agent session-state lane (Issue #889).

A TTL-bounded key/value store for ephemeral agent run-state, kept in a
**dedicated** table — NOT in ``memories`` — so it never pollutes the knowledge
``recall()`` space. Run state is not embedded and not semantically searched; it
is structurally excluded from recall by living in its own table.

One value per ``(context_id, key)``; ``set_state`` upserts on that pair. TTL is
expressed via ``expires_at`` + a lazy read filter (expired rows are filtered on
read and swept opportunistically), following the established precedent of
``file_objects.expires_at`` (#485) and ``neural`` calibration ``is_expired()``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AgentState(Base):
    """One row per ``(context_id, key)`` agent-state entry (Issue #889).

    ``value`` holds an arbitrary JSON value (object, array, or scalar). The
    column is intentionally NOT indexed for content search — this lane is a
    keyed run-state store, not a knowledge store.
    """

    __tablename__ = "agent_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    # Naive UTC by convention (TIMESTAMP WITHOUT TIME ZONE); the engine pins the
    # session to UTC so utcnow() values round-trip unambiguously. NULL = no TTL
    # (the entry lives until explicitly deleted).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # One value per key per context; set_state upserts on this pair.
        Index(
            "uq_agent_states_context_key",
            "context_id",
            "key",
            unique=True,
        ),
        # Lazy-reap helper: a partial index over only the rows that carry a TTL,
        # so the expiry sweep stays proportional to TTL'd rows, not the table.
        Index(
            "idx_agent_states_expires",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )
