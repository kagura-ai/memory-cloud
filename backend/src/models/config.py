"""Configuration Models.

Issue #130: Context-scoped Search & Reranker Settings UI
Issue #160: Renamed from Project to Context
Issue #363: Config value storage in database
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DECIMAL,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from db.base import Base


class ConfigOverride(Base):
    """Runtime configuration override.

    Issue #363: Stores admin-set config values that override environment variables.
    Values persist across container restarts (stored in PostgreSQL).
    """

    __tablename__ = "config_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(2000), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ContextSearchConfig(Base):
    """Context-specific hybrid search and reranker configuration.

    Each context can configure:
    - Hybrid search weights (semantic vs BM25)
    - Fetch factor (candidate retrieval multiplier)
    - Reranker provider and model
    - Reranker enable/disable

    Issue #130: Context-scoped Search & Reranker Settings UI
    Issue #160: Renamed from ProjectSearchConfig to ContextSearchConfig

    Attributes:
        id: Primary key
        context_id: Foreign key to contexts table (UNIQUE, CASCADE DELETE)
        semantic_weight: Weight for semantic (embedding) search (0.0-1.0)
        bm25_weight: Weight for keyword (BM25) search (0.0-1.0)
        fetch_factor: Candidate retrieval multiplier (1-10)
        use_rerank: Enable/disable reranking
        reranker_provider: Reranker provider ('voyage' or 'cohere')
        reranker_model: Provider-specific model name
        created_at: Creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "context_search_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Hybrid Search weights
    semantic_weight: Mapped[Decimal] = mapped_column(DECIMAL(3, 2), nullable=False, default=0.60)
    bm25_weight: Mapped[Decimal] = mapped_column(DECIMAL(3, 2), nullable=False, default=0.40)

    # Fetch factor
    fetch_factor: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Reranker settings
    use_rerank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reranker_provider: Mapped[str | None] = mapped_column(String(20), default="voyage")
    reranker_model: Mapped[str | None] = mapped_column(String(50), default="rerank-2")

    # Embedding configuration (Issue #146: Immutable after context creation)
    embedding_model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="text-embedding-3-small"
    )
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=512)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "semantic_weight >= 0.0 AND semantic_weight <= 1.0",
            name="semantic_weight_range",
        ),
        CheckConstraint(
            "bm25_weight >= 0.0 AND bm25_weight <= 1.0",
            name="bm25_weight_range",
        ),
        CheckConstraint(
            "fetch_factor >= 1 AND fetch_factor <= 10",
            name="fetch_factor_range",
        ),
        CheckConstraint(
            "ABS(semantic_weight + bm25_weight - 1.0) < 0.01",
            name="weights_sum_check",
        ),
        CheckConstraint(
            "reranker_provider IN ('voyage', 'cohere', 'ollama')",
            name="reranker_provider_check",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<ContextSearchConfig("
            f"context_id={self.context_id}, "
            f"semantic={self.semantic_weight}, "
            f"bm25={self.bm25_weight}, "
            f"fetch_factor={self.fetch_factor}, "
            f"use_rerank={self.use_rerank}, "
            f"provider={self.reranker_provider}, "
            f"model={self.reranker_model}"
            f")>"
        )
