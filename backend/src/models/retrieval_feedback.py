"""Retrieval feedback signal (Issue #888, part of epic #885).

An **append-only event log** of "this recalled memory was / was not useful for
this query" signals. Kept in a **dedicated** table — NOT in ``memories`` — so it
never pollutes the knowledge ``recall()`` space (separate table, never embedded,
structurally excluded from search), the same isolation principle as
``agent_states`` (#889).

Append-only by design: feedback is a time series (a user may change their mind, or
repeat a signal — repetition is itself signal), so there is no unique constraint
and no upsert. Aggregation (net-helpful score) is a read-time concern, not a
write-time dedup. ``user_id`` is recorded so feedback is attributable (abuse
tracing + future signal weighting).

Both FKs cascade on delete so feedback is erased with its context or its memory —
keeping the lane consistent with GDPR/APPI erasure (a context delete removes its
feedback automatically).

This signal is the prerequisite for any future Eval→Skill self-update loop, which
MUST stay gated behind the golden retrieval eval harness (#344) — see
``backend/tests/eval/README.md``. No auto-promotion / self-update loop ships
before that eval gate is green.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Query text is stored truncated to this length — the signal needs the query for
# offline analysis, not unbounded text retention (CIO gate1 note).
QUERY_MAX_LEN = 1024


class RetrievalFeedback(Base):
    """One append-only feedback event: was ``memory_id`` useful for ``query``?"""

    __tablename__ = "retrieval_feedback"

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
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The recall query this feedback is about (optional — a caller may rate a
    # memory without echoing the query). Stored truncated; never embedded.
    query: Mapped[str | None] = mapped_column(String(QUERY_MAX_LEN), nullable=True)
    helpful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional free-text note (e.g. why it was wrong). Truncation enforced in the
    # service layer; Text column keeps the model permissive.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Naive UTC by convention (engine pins the session to UTC).
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Aggregation reads (net-helpful per memory within a context) scan by
        # this pair — the append-only log has no unique key.
        Index("idx_retrieval_feedback_context_memory", "context_id", "memory_id"),
    )
