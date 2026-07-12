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
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from db.base import Base

# Router calibration vocabulary (#1220). Buckets are the classifier's lane
# decisions; arms are the retrieval strategies measured against each bucket.
ROUTER_CALIBRATION_BUCKETS = ("keyword", "semantic", "hybrid")
ROUTER_CALIBRATION_ARMS = ("keyword", "semantic", "hybrid", "routed")
ROUTER_CALIBRATION_SOURCE_FROZEN = "frozen_corpus"
ROUTER_CALIBRATION_SOURCE_LIVE = "live_traffic"


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
        reranker_provider: Reranker provider ('voyage', 'cohere', or 'self_hosted')
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

    # Issue #1048: bounded reinforce re-ranking (adoption + retrieval feedback).
    # Issue #1207: default ON for newly created rows — the pre-registered
    # kagura-memory-eval program attributed the update-correctness headline
    # (+0.36 conditional lift over vanilla RAG, BCa 95% [0.24, 0.50]) entirely
    # to this bounded, LLM-free, fail-safe re-rank, so fresh contexts get it
    # without discovering a flag. Existing rows are NOT rewritten — and since
    # rows are auto-stamped at context creation (and by any past recall's
    # create_or_get) under the old default, pre-#1207 contexts hold a stored
    # ``false`` and stay off until enabled via update_search_config. Only the
    # rare row-less legacy context adopts the new default lazily when the
    # search path materializes its row.
    # ``reinforce_max_boost`` bounds the per-result multiplicative adjustment
    # to [1-boost, 1+boost] so semantic relevance always dominates (the
    # re-rank only reorders the relevance-filtered candidate pool).
    reinforce_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    reinforce_max_boost: Mapped[Decimal] = mapped_column(
        DECIMAL(3, 2), nullable=False, server_default="0.15", default=Decimal("0.15")
    )
    # Issue #1065: forge-resistant mode. When true, the reinforce re-rank counts
    # ONLY host-arbitrated feedback (provenance='host') — an untrusted agent's
    # self-emitted feedback(helpful=True) can no longer move ranking. Default OFF
    # preserves #1048 behaviour (all feedback counts). Enable on contexts exposed
    # to untrusted autonomous agents (e.g. trust_tier='external').
    # Threat-model note: this flag is itself editable via update_search_config,
    # so it is only meaningful when set out-of-band by an operator/cockpit and the
    # untrusted agent lacks EDITOR/OWNER on the context (else it could flip it off).
    reinforce_require_host_arbitration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # Issue #1212: query-intent retrieval router (experiment-gated).
    # 'off' (default): router never runs. 'log_only': classifier decision is
    # stamped into recall telemetry with ZERO ranking change. 'active': the
    # routed lane is used ONLY when the caller did not pass search_mode
    # explicitly (an explicit search_mode always wins). The default stays
    # 'off' until the stage-3 calibration gate (frozen 300-doc corpus) shows
    # the router beating semantic-only — the same bar hybrid failed (#1212).
    routing_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="off", default="off"
    )

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
            "reranker_provider IN ('voyage', 'cohere', 'self_hosted')",
            name="reranker_provider_check",
        ),
        CheckConstraint(
            "routing_mode IN ('off', 'log_only', 'active')",
            name="routing_mode_check",
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


class RouterCalibration(Base):
    """Per-bucket router arm performance (#1220 stage 4).

    One row = one (bucket, arm) measurement: how one retrieval strategy
    performed on the queries the classifier routes to one lane. Rows with
    ``context_id IS NULL`` are the fleet defaults measured on the frozen
    eval corpus (written by ``tests.eval.router_gate_runner``); rows with a
    ``context_id`` let managed-cloud tuning diverge per context from
    live-traffic measurements without touching the self-host defaults —
    the same NULL-vs-non-NULL keying as ``embedding_calibrations``.

    Attributes:
        id: Primary key.
        context_id: Context scope; NULL = fleet default (frozen corpus).
        bucket: Classifier lane the measured queries were routed to.
        arm: Retrieval strategy measured on that bucket.
        p_at_5: Mean P@5 of the arm on the bucket.
        mrr_at_10: MRR@10 of the arm on the bucket.
        n_queries: Number of queries behind the measurement.
        source: Where the measurement came from (frozen_corpus | live_traffic).
        sampled_at: When the measurement was taken.
    """

    __tablename__ = "router_calibrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False)
    p_at_5: Mapped[float] = mapped_column(Float, nullable=False)
    mrr_at_10: Mapped[float] = mapped_column(Float, nullable=False)
    n_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ROUTER_CALIBRATION_SOURCE_FROZEN
    )
    sampled_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "bucket IN ('keyword', 'semantic', 'hybrid')",
            name="router_calibrations_bucket_check",
        ),
        CheckConstraint(
            "arm IN ('keyword', 'semantic', 'hybrid', 'routed')",
            name="router_calibrations_arm_check",
        ),
        CheckConstraint(
            "source IN ('frozen_corpus', 'live_traffic')",
            name="router_calibrations_source_check",
        ),
        CheckConstraint(
            "p_at_5 >= 0.0 AND p_at_5 <= 1.0",
            name="router_calibrations_p_at_5_range",
        ),
        CheckConstraint(
            "mrr_at_10 >= 0.0 AND mrr_at_10 <= 1.0",
            name="router_calibrations_mrr_range",
        ),
        CheckConstraint(
            "n_queries >= 0",
            name="router_calibrations_nonneg_n",
        ),
        # One measurement per (scope, bucket, arm, source): partial-unique
        # split on NULL context (the embedding_calibrations pattern —
        # Postgres unique treats NULLs as distinct, so the global scope
        # needs its own predicate index).
        Index(
            "uq_router_calibration_global",
            "bucket",
            "arm",
            "source",
            unique=True,
            postgresql_where=text("context_id IS NULL"),
        ),
        Index(
            "uq_router_calibration_context",
            "context_id",
            "bucket",
            "arm",
            "source",
            unique=True,
            postgresql_where=text("context_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<RouterCalibration("
            f"context_id={self.context_id}, "
            f"bucket={self.bucket}, "
            f"arm={self.arm}, "
            f"p@5={self.p_at_5}, "
            f"n={self.n_queries}, "
            f"source={self.source}"
            f")>"
        )
