"""SQLAlchemy models for Neural Memory configuration.

Issue #107: Move neural config from env vars to database
Issue #406: Add EmbeddingCalibration for knn_seed percentile calibration
"""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import expression

from db.base import Base


class NeuralConfig(Base):
    """Neural Memory configuration stored in database.

    Allows admin-editable configuration for Neural Memory system
    without requiring environment variable changes or restarts.

    Attributes:
        id: Primary key
        key: Configuration parameter name (unique)
        value: Configuration value (stored as string)
        value_type: Type hint for parsing (float, int, bool)
        category: Category for UI grouping
        description: Human-readable description
        min_value: Minimum allowed value (for validation)
        max_value: Maximum allowed value (for validation)
        created_at: Creation timestamp
        updated_at: Last modification timestamp
    """

    __tablename__ = "neural_config"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Configuration key-value
    key = Column(String(64), nullable=False, unique=True, index=True)
    value = Column(String(255), nullable=False)
    value_type = Column(String(16), nullable=False, default="float")
    category = Column(String(32), nullable=False, default="general", index=True)

    # Metadata
    description = Column(Text, nullable=True)  # TEXT to match migration
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<NeuralConfig(key='{self.key}', value='{self.value}')>"

    def get_typed_value(self) -> float | int | bool | str:
        """Get value converted to its proper type."""
        if self.value_type == "float":
            return float(self.value)
        elif self.value_type == "int":
            return int(self.value)
        elif self.value_type == "bool":
            return self.value.lower() in ("true", "1", "yes")
        return self.value

    def validate_value(self, new_value: str) -> tuple[bool, str | None]:
        """Validate a new value against min/max constraints.

        Args:
            new_value: Value to validate (as string)

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            if self.value_type == "float":
                val = float(new_value)
            elif self.value_type == "int":
                val = int(new_value)
            elif self.value_type == "bool":
                # Any string is valid for bool
                return True, None
            else:
                return True, None

            # Check min/max
            if self.min_value is not None and val < self.min_value:
                return False, f"Value must be >= {self.min_value}"
            if self.max_value is not None and val > self.max_value:
                return False, f"Value must be <= {self.max_value}"

            return True, None

        except ValueError as e:
            return False, f"Invalid {self.value_type} value: {e}"


class EmbeddingCalibration(Base):
    """Per-model percentile calibration of top-k neighbor cosine similarity.

    Issue #406 / #240 Phase B. Stores the 5-number summary of the distribution
    of top-k neighbor cosine similarity for a given embedding model. Written
    by the calibration job (bootstrap / admin-manual / lazy-TTL triggers),
    read by ``neural.calibration.resolve_knn_threshold`` at ``remember()``
    time to set the runtime ``semantic_similarity`` seeding threshold
    without hardcoding a per-model value.

    ``context_id`` is nullable:

    - ``NULL`` = model-global calibration. At most one row per ``(model_name,
      dimensions)`` combination (enforced by partial unique index
      ``uq_calibration_model_dims_global``).
    - non-NULL = per-context calibration (D5 v2). Schema allows it, but the
      v1 runtime lookup only reads the global row — see the ``TODO(v2)``
      in ``neural/calibration.py``. Per-(model, dims, context_id) uniqueness
      is enforced by ``uq_calibration_model_dims_nonnull``.

    ``valid_until`` drives the lazy TTL recalibration trigger. Default TTL
    is ``NeuralMemoryConfig.calibration_ttl_days`` (30 days in v1), computed
    by the calibration job and stored absolute so readers don't need to
    recompute. Kept in app config rather than hardcoded in the migration so
    #407 Task 2's drift-measurement can tune it via env var without a schema
    change.

    Attributes:
        id: Primary key (UUID)
        model_name: Embedding model name (e.g. "text-embedding-3-small")
        dimensions: Vector dimensionality (e.g. 512, 4096)
        context_id: Optional context UUID; NULL = model-global
        p25-p99: Percentile values from top-k neighbor distribution
        sample_size: Number of observations (top-k calls * k) used to fit
        sampled_at: When the calibration was computed
        valid_until: sampled_at + calibration_ttl_days; lazy-recal trigger
    """

    __tablename__ = "embedding_calibrations"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=expression.text("gen_random_uuid()"),
    )
    model_name = Column(String(100), nullable=False)
    dimensions = Column(Integer, nullable=False)
    context_id = Column(UUID(as_uuid=True), nullable=True)

    p25 = Column(Float, nullable=False)
    p50 = Column(Float, nullable=False)
    p75 = Column(Float, nullable=False)
    p90 = Column(Float, nullable=False)
    p95 = Column(Float, nullable=False)
    p99 = Column(Float, nullable=False)

    sample_size = Column(Integer, nullable=False)
    sampled_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    valid_until = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "p25 >= 0.0 AND p25 <= 1.0 AND "
            "p50 >= 0.0 AND p50 <= 1.0 AND "
            "p75 >= 0.0 AND p75 <= 1.0 AND "
            "p90 >= 0.0 AND p90 <= 1.0 AND "
            "p95 >= 0.0 AND p95 <= 1.0 AND "
            "p99 >= 0.0 AND p99 <= 1.0",
            name="embedding_calibrations_percentiles_in_range",
        ),
        CheckConstraint(
            "sample_size >= 0",
            name="embedding_calibrations_nonneg_sample_size",
        ),
        CheckConstraint(
            "valid_until > sampled_at",
            name="embedding_calibrations_valid_until_future",
        ),
        Index(
            "uq_calibration_model_dims_global",
            "model_name",
            "dimensions",
            unique=True,
            postgresql_where=expression.text("context_id IS NULL"),
        ),
        Index(
            "uq_calibration_model_dims_nonnull",
            "model_name",
            "dimensions",
            "context_id",
            unique=True,
            postgresql_where=expression.text("context_id IS NOT NULL"),
        ),
    )

    # Ordered percentile pairs for percentile() linear interpolation.
    _STORED_PERCENTILES: tuple[tuple[float, str], ...] = (
        (25.0, "p25"),
        (50.0, "p50"),
        (75.0, "p75"),
        (90.0, "p90"),
        (95.0, "p95"),
        (99.0, "p99"),
    )

    def percentile(self, p: float) -> float:
        """Interpolate a percentile from the stored 5-number summary.

        Linear interpolation between the two stored percentiles bracketing
        ``p``. The stored grid is ``[25, 50, 75, 90, 95, 99]``. For ``p``
        outside this range (below 25 or above 99), the nearest stored value
        is returned — extrapolation on a probability is not meaningful for
        this use case (the runtime threshold falls back to the D2 floor for
        degenerate distributions anyway).

        Args:
            p: Target percentile in ``[0.0, 100.0]``. Common value is 90.0.

        Returns:
            Interpolated similarity value in ``[0.0, 1.0]`` (same range as
            the stored percentiles; never extrapolates outside).
        """
        if p <= self._STORED_PERCENTILES[0][0]:
            return float(getattr(self, self._STORED_PERCENTILES[0][1]))
        if p >= self._STORED_PERCENTILES[-1][0]:
            return float(getattr(self, self._STORED_PERCENTILES[-1][1]))
        for i in range(len(self._STORED_PERCENTILES) - 1):
            lo_p, lo_attr = self._STORED_PERCENTILES[i]
            hi_p, hi_attr = self._STORED_PERCENTILES[i + 1]
            if lo_p <= p <= hi_p:
                lo_v = float(getattr(self, lo_attr))
                hi_v = float(getattr(self, hi_attr))
                if hi_p == lo_p:
                    return lo_v
                frac = (p - lo_p) / (hi_p - lo_p)
                return lo_v + frac * (hi_v - lo_v)
        # Unreachable given the bounds checks above, but keep the type checker happy.
        return float(self.p90)

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True iff the calibration is past its ``valid_until``.

        Used by the lazy-TTL recalibration trigger in
        ``neural.calibration.resolve_knn_threshold``: an expired row still
        serves its stored value (fail-open on stale calibration), while the
        lookup enqueues a background recalibration task (deduped).
        """
        current = now if now is not None else datetime.now(UTC)
        valid_until = self.valid_until
        # SQLAlchemy returns naive datetimes for some adapters; coerce to UTC
        # to make comparison deterministic.
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        return current >= valid_until

    def __repr__(self) -> str:
        ctx = "global" if self.context_id is None else str(self.context_id)[:8]
        return (
            f"<EmbeddingCalibration(model={self.model_name!r}, "
            f"dims={self.dimensions}, context={ctx}, p90={self.p90:.4f})>"
        )
