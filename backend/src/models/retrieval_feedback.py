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

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Query text is stored truncated to this length — the signal needs the query for
# offline analysis, not unbounded text retention (CIO gate1 note).
QUERY_MAX_LEN = 1024
# Free-text note cap. The column is Text (unbounded at the DB), so this is the
# single source of truth enforced at every boundary (API schema, MCP handler,
# service truncation) to avoid silent truncation / unbounded request bodies.
NOTE_MAX_LEN = 2000

# Issue #1065: server-stamped provenance of a feedback signal. ``agent`` is the
# default — the signal an autonomous caller emits via feedback(), which a
# hijacked / prompt-injected agent can forge to manufacture its own ranking
# boost. ``host`` is the forge-resistant variant: stamped server-side as
# originating from the trusted host/cockpit and backed by an INDEPENDENT verdict
# (a check/test result or an operator HITL approval), never the agent's
# self-report. The agent-callable feedback() path can only ever produce ``agent``
# (the public schema does not expose this field); only the host-arbitration seam
# stamps ``host``. Recall ranking (#1048) can be configured to weight only the
# unforgeable signal for untrusted callers.
FEEDBACK_PROVENANCE_AGENT = "agent"
FEEDBACK_PROVENANCE_HOST = "host"
_ALL_FEEDBACK_PROVENANCES: tuple[str, ...] = (
    FEEDBACK_PROVENANCE_AGENT,
    FEEDBACK_PROVENANCE_HOST,
)


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
    # Issue #1065: server-stamped provenance — 'agent' (forgeable self-report,
    # the default) vs 'host' (host/cockpit-arbitrated, backed by an independent
    # verdict). Server-authoritative: clients cannot set it (not on the public
    # schema), so an agent's signal is always 'agent'.
    provenance: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=FEEDBACK_PROVENANCE_AGENT,
        default=FEEDBACK_PROVENANCE_AGENT,
    )
    # Naive UTC by convention (engine pins the session to UTC).
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Aggregation reads (net-helpful per memory within a context) scan by
        # this pair — the append-only log has no unique key.
        Index("idx_retrieval_feedback_context_memory", "context_id", "memory_id"),
        # Provenance is a closed enum — reject anything but the known kinds so a
        # bad value can never silently slip past the forge-resistant aggregation.
        # CHECK derived from _ALL_FEEDBACK_PROVENANCES (single source of truth,
        # mirrors memory.py:source_type). Byte-identical to the migration literal.
        CheckConstraint(
            f"provenance IN ({', '.join(repr(p) for p in _ALL_FEEDBACK_PROVENANCES)})",
            name="valid_retrieval_feedback_provenance",
        ),
    )
