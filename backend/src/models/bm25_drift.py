"""SQLAlchemy model for BM25 IDF drift observability.

Issue #343: One row per context per cron cycle, capturing the PSI score
between memory-only and collection-global IDF distributions.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Bm25IdfDriftLog(Base):
    """A single PSI measurement for a context's BM25 IDF distribution.

    Attributes:
        id: Auto-increment primary key (high-frequency append-only)
        context_id: Target context (FK with CASCADE on context delete)
        measured_at: Cron cycle timestamp (tz-aware)
        psi: Population Stability Index. NULL iff status is insufficient_data
        psi_status: One of insufficient_data / stable / minor_drift / significant_drift
        m_memory_points: Memory-source point count at measurement time
        r_resource_points: Resource-source point count at measurement time
        num_terms: Surviving term count after Cochran's df>=5 filter
        top_divergent_terms: JSONB list of {index, df_memory, df_global, ...}
            where index is the mmh3 token hash (not plaintext)
    """

    __tablename__ = "bm25_idf_drift_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    psi: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    psi_status: Mapped[str] = mapped_column(String(30), nullable=False)
    m_memory_points: Mapped[int] = mapped_column(Integer, nullable=False)
    r_resource_points: Mapped[int] = mapped_column(Integer, nullable=False)
    num_terms: Mapped[int] = mapped_column(Integer, nullable=False)
    top_divergent_terms: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "psi_status IN ('insufficient_data', 'stable', 'minor_drift', 'significant_drift')",
            name="valid_bm25_idf_drift_status",
        ),
        CheckConstraint(
            "(psi_status = 'insufficient_data') = (psi IS NULL)",
            name="bm25_idf_drift_psi_null_iff_insufficient",
        ),
        CheckConstraint(
            "m_memory_points >= 0 AND r_resource_points >= 0 AND num_terms >= 0",
            name="bm25_idf_drift_nonneg_counts",
        ),
        # DESC ordering on measured_at must match the migration (a98) so
        # alembic autogenerate does not flag a spurious diff.
        Index(
            "idx_bm25_idf_drift_context_time",
            "context_id",
            text("measured_at DESC"),
        ),
        Index(
            "idx_bm25_idf_drift_measured_at",
            text("measured_at DESC"),
        ),
        Index(
            "idx_bm25_idf_drift_alerted",
            "psi_status",
            text("measured_at DESC"),
            postgresql_where=text("psi_status IN ('minor_drift', 'significant_drift')"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Bm25IdfDriftLog(id={self.id}, context_id={self.context_id}, "
            f"psi={self.psi}, status={self.psi_status})>"
        )
