"""HOW-MUCH axis: measurement history lane (Issue #1333).

An append-only numeric time-series store, kept in a **dedicated** table — NOT
in ``memories`` — so it never pollutes the knowledge ``recall()`` space and,
critically, so Sleep consolidation can never merge or rewrite a series.
Measurements are not embedded and not semantically searched; they are
structurally excluded from recall by living in their own table.

One row per observation of ``(context_id, metric)`` at ``measured_at``.
Milestone-marker memories ("hit goal weight") remain a caller concern via
``remember()`` — this lane stores only the raw numbers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Measurement(Base):
    """One append-only measurement observation (Issue #1333).

    ``value`` is ``Numeric`` (exact, no float drift at rest); aggregation
    casts to float8 at read time. ``details`` holds free-form observation
    metadata (device, source, notes) — intentionally NOT indexed for content
    search; this lane is a numeric series store, not a knowledge store.
    """

    __tablename__ = "measurements"

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
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    # Naive UTC by convention (TIMESTAMP WITHOUT TIME ZONE); the engine pins
    # the session to UTC so utils.datetime.utcnow() values round-trip
    # unambiguously. Observation time — distinct from created_at (row insert
    # time) so backdated imports keep their true series position.
    measured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Series scan: recall_series filters on (context_id, metric) and
        # bucket-orders by measured_at — one index covers the whole read path.
        Index(
            "idx_measurements_context_metric_measured_at",
            "context_id",
            "metric",
            "measured_at",
        ),
    )
