"""SQLAlchemy model for LLM pricing master table.

Issue #471: append-only price snapshot table that powers cost-grade
reporting on top of ``sleep_report_llm_usage`` (and, in #474, the broader
``llm_call_log`` ledger). Rows are NEVER updated or deleted in normal
operation — a price change inserts a new row with a later
``effective_from``, and the cost-aggregation join picks the row whose
``effective_from <= started_at`` of the consuming sleep run / call. This
keeps historical reports reproducible across rate changes.

Why a generalized ``unit_type`` axis: per-token billing (Anthropic /
OpenAI / Google) and per-search-unit billing (Cohere rerank, $2 / 1k SU)
do not share a base unit. Storing them in separate tables would split
the join in #472. Instead this table carries:

- ``unit_type`` enum that names the dimension being priced
  (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
  ``cache_write_tokens``, ``embedding_tokens``, ``rerank_tokens``,
  ``rerank_search_units``).
- ``unit_denominator`` to express "per N units" in the same column for
  every row — Anthropic / OpenAI / Google rows store ``1_000_000``
  (per 1M tokens), Cohere rerank rows store ``1_000`` (per 1k search
  units). Cost = ``input_tokens * price_per_unit / unit_denominator``.

The reranker enum values exist from day 1 even though #471 does not seed
any rerank rows. #474 will seed them without a CHECK-constraint
migration.

Tier breakpoints are encoded as multiple rows for the same
``(provider, model, unit_type, effective_from)`` differing only by
``context_min_tokens`` (and optionally ``context_max_tokens``). The PK
includes ``context_min_tokens`` so multiple tiers coexist. Gemini 2.5
Pro doubles past 200k context — that scenario is exactly two rows with
``context_min_tokens=0, context_max_tokens=200000`` and
``context_min_tokens=200000, context_max_tokens=NULL``.

``currency`` defaults to ``USD`` and exists from day 1 even though
multi-currency display is OOS for #471. Adding the column now costs one
default value; adding it later would force an ALTER TABLE on a
production-sized table.

``unit_type`` is a text + CHECK constraint (intentionally NOT a
PostgreSQL ENUM) — the rest of this codebase uses the same pattern
(see ``sleep_reports.status``, ``erasure_requests.status`` etc) because
PG ENUMs are painful to ALTER and don't play nicely with alembic.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Public list of allowed ``unit_type`` values. Imported by:
# - The CHECK constraint below.
# - Migrations that seed rows.
# - ``services/llm_pricing_service.py`` for input validation (defense in
#   depth — the DB CHECK is the source of truth).
LLM_PRICING_UNIT_TYPES: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "embedding_tokens",
    "rerank_tokens",
    "rerank_search_units",
)


class LLMPricing(Base):
    """Append-only price snapshot for an LLM/embedding/rerank model unit.

    See module docstring for the design rationale (append-only,
    ``effective_from``-driven temporal join, ``unit_denominator`` to bridge
    per-1M-tokens and per-1k-search-units, ``currency`` forward-compat,
    text + CHECK enum).

    Lookup pattern (implemented in ``services/llm_pricing_service.py``):

        SELECT * FROM llm_pricing
        WHERE provider = :provider
          AND model = :model
          AND unit_type = :unit_type
          AND effective_from <= :started_at
          AND context_min_tokens <= :context_tokens
          AND (context_max_tokens IS NULL OR context_max_tokens > :context_tokens)
        ORDER BY effective_from DESC, context_min_tokens DESC
        LIMIT 1;

    A miss returns ``None``; the caller is expected to write
    ``cost_usd = NULL`` for that breakdown row, which the UI surfaces as
    "cost unknown" (the same treatment as legacy NULL-model rows from
    before this issue).
    """

    __tablename__ = "llm_pricing"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Naive ``DateTime`` to match the codebase convention (everything stored
    # in UTC; see ``sleep_reports.started_at`` and the rest of
    # ``backend/src/models/``). Mixing tz-aware and tz-naive datetimes in
    # SQLAlchemy comparisons raises TypeError at the Python layer, so the
    # column type must agree with the join target. Callers pass naive UTC
    # datetimes (e.g. ``utils.datetime.utcnow()``).
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Tier breakpoint columns. ``context_min_tokens`` defaults to 0 (no
    # lower bound). ``context_max_tokens`` is nullable for "no upper bound";
    # most rows have NULL here. Gemini 2.5 Pro is the canonical multi-tier
    # case in the 2026-04-28 seed.
    context_min_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    context_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # NUMERIC(14, 10) gives 4 digits before the decimal and 10 after — enough
    # for prices like 25.0000000000 (Claude Opus output, $25/1M) down to
    # 0.0000200000 (text-embedding-3-small, $0.02/1M) without rounding.
    pricing_model: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="per_token",
        server_default="per_token",
    )

    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(14, 10), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="USD",
        server_default=text("'USD'"),
    )

    # Bridges per-1M-tokens (1_000_000) and per-1k-search-units (1_000)
    # so both fit the same row shape. Default 1_000_000 matches the
    # majority-case (token-based providers).
    unit_denominator: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1_000_000,
        server_default=text("1000000"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Composite uniqueness — multiple rows for the same
        # (provider, model, unit_type, effective_from) only differ by
        # ``context_min_tokens`` (multi-tier), and we never want two rows
        # with identical (provider, model, unit_type, effective_from,
        # context_min_tokens) since the lookup query would be ambiguous.
        UniqueConstraint(
            "provider",
            "model",
            "unit_type",
            "effective_from",
            "context_min_tokens",
            name="uq_llm_pricing_lookup_key",
        ),
        CheckConstraint(
            "unit_type IN ('input_tokens', 'output_tokens', "
            "'cache_read_tokens', 'cache_write_tokens', "
            "'embedding_tokens', 'rerank_tokens', 'rerank_search_units')",
            name="valid_llm_pricing_unit_type",
        ),
        CheckConstraint(
            "pricing_model IN ('per_token', 'subscription', 'hybrid')",
            name="valid_llm_pricing_model",
        ),
        CheckConstraint(
            "price_per_unit >= 0",
            name="valid_llm_pricing_price_nonneg",
        ),
        CheckConstraint(
            "unit_denominator > 0",
            name="valid_llm_pricing_unit_denominator_positive",
        ),
        CheckConstraint(
            "context_min_tokens >= 0",
            name="valid_llm_pricing_context_min_nonneg",
        ),
        CheckConstraint(
            "context_max_tokens IS NULL OR context_max_tokens > context_min_tokens",
            name="valid_llm_pricing_context_max_gt_min",
        ),
        # NOTE: no explicit ``idx_llm_pricing_lookup`` — PostgreSQL's
        # auto-created index for ``uq_llm_pricing_lookup_key`` covers the
        # lookup query as a prefix index (provider, model, unit_type,
        # effective_from, context_min_tokens). An additional 4-column
        # index would just duplicate write cost and storage with no
        # measured query benefit. Add an explicit index later if EXPLAIN
        # ANALYZE on #472's queries shows the prefix index isn't being
        # picked up.
    )

    def __repr__(self) -> str:
        return (
            f"<LLMPricing({self.provider}/{self.model} "
            f"{self.unit_type}=${self.price_per_unit}/{self.unit_denominator} "
            f"{self.currency} from={self.effective_from})>"
        )
