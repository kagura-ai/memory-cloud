"""LLM pricing lookup service (Issue #471).

Resolves the price-per-unit row from ``llm_pricing`` for a given
``(provider, model, unit_type)`` tuple at a specific point in time and
context-token tier. Used by the sleep reporter (and, in #474, the
broader cost ledger) to compute ``cost_usd`` from token counts.

Design rationale:

- Returns ``LLMPricing | None`` — never raises on miss. Callers treat
  missing pricing as ``cost_usd = NULL`` ("cost unknown") so a
  newly-deployed model that hasn't been seeded yet still produces a
  valid ``sleep_report_llm_usage`` row; only the cost number is blank.
  This matches the ``RerankerService`` / ``EmbeddingService`` pattern
  for graceful absence.

- Uses the append-only ``effective_from`` snapshot to keep historical
  reports reproducible: a re-run of last month's cost aggregation
  produces the same ``$`` figure regardless of any rate changes that
  shipped in between.

- Tier breakpoints (Gemini 2.5 Pro: 0..200k vs >200k) are encoded as
  multiple rows for the same ``(provider, model, unit_type,
  effective_from)`` differing by ``context_min_tokens``. The lookup
  picks the tier whose ``[context_min_tokens, context_max_tokens)``
  interval contains the run's ``context_tokens``.

- Class-based with ``AsyncSession`` injected in ``__init__``, matching
  the rest of ``backend/src/services/`` (compare ``RerankerService``,
  ``EmbeddingService``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_pricing import LLM_PRICING_UNIT_TYPES, LLMPricing
from utils.logger import get_logger

logger = get_logger(__name__)


class LLMPricingService:
    """Resolve price-per-unit rows from the ``llm_pricing`` master table.

    Stateless from the caller's perspective — the only mutable state is
    the injected ``AsyncSession``. Reuse one service instance per request
    / sleep run; each ``lookup()`` call is one DB round-trip.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the service.

        Args:
            db: Async SQLAlchemy session; the same session pattern as
                every other service in this layer.
        """
        self.db = db

    async def lookup(
        self,
        *,
        provider: str,
        model: str,
        unit_type: str,
        started_at: datetime,
        context_tokens: int = 0,
    ) -> LLMPricing | None:
        """Resolve the pricing row active for a call.

        Args:
            provider: Provider identifier (e.g. ``'anthropic'``,
                ``'openai'``, ``'ollama'``).
            model: Model identifier (e.g. ``'claude-sonnet-4-6'``,
                ``'text-embedding-3-small'``).
            unit_type: Pricing dimension — one of
                ``LLM_PRICING_UNIT_TYPES``.
            started_at: When the consuming run / call began. Used to
                pick the snapshot row whose ``effective_from <= started_at``,
                which keeps historical reports reproducible across rate
                changes.
            context_tokens: Total context-window tokens for the call;
                used to pick the right tier when a model has multiple
                tier rows (default 0, i.e. the lowest tier).

        Returns:
            The matching ``LLMPricing`` row, or ``None`` if no row
            matches. The latter is the documented "cost unknown" path —
            callers should write ``cost_usd = NULL`` and let the UI
            render that as a "cost unknown" cell.
        """
        # Defense in depth — the DB has the same CHECK, but failing
        # fast at the service boundary turns a typo into a clear
        # ValueError instead of an unrelated 500 from the COMMIT path.
        if unit_type not in LLM_PRICING_UNIT_TYPES:
            raise ValueError(
                f"Invalid unit_type {unit_type!r}; must be one of {LLM_PRICING_UNIT_TYPES}"
            )

        # Pick the most-recent ``effective_from`` row whose tier
        # contains ``context_tokens``. Sorting on
        # ``context_min_tokens DESC`` after ``effective_from DESC``
        # means a multi-tier model returns the *highest matching*
        # tier — e.g. for Gemini 2.5 Pro at 250k tokens, the
        # ``context_min_tokens=200000`` row wins over the
        # ``context_min_tokens=0`` row.
        stmt = (
            select(LLMPricing)
            .where(
                and_(
                    LLMPricing.provider == provider,
                    LLMPricing.model == model,
                    LLMPricing.unit_type == unit_type,
                    LLMPricing.effective_from <= started_at,
                    LLMPricing.context_min_tokens <= context_tokens,
                    or_(
                        LLMPricing.context_max_tokens.is_(None),
                        LLMPricing.context_max_tokens > context_tokens,
                    ),
                )
            )
            .order_by(
                LLMPricing.effective_from.desc(),
                LLMPricing.context_min_tokens.desc(),
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one_or_none()

        if row is None:
            # Debug-level: misses are expected (new model, exotic
            # provider, typo). Caller writes cost_usd=NULL.
            logger.debug(
                "llm_pricing_lookup_miss",
                provider=provider,
                model=model,
                unit_type=unit_type,
                started_at=started_at.isoformat(),
                context_tokens=context_tokens,
            )

        return row

    async def compute_cost_usd(
        self,
        *,
        provider: str,
        model: str,
        unit_type: str,
        started_at: datetime,
        units: int,
        context_tokens: int = 0,
    ) -> float | None:
        """Convenience wrapper: lookup price + multiply by units.

        Equivalent to ``lookup(...)`` followed by
        ``units * row.price_per_unit / row.unit_denominator``. Returns
        ``None`` on a lookup miss (caller writes ``cost_usd = NULL``).

        Floats are sufficient for cost computation at this granularity —
        ``Decimal`` would buy precision we don't need (cost rows in
        ``sleep_reports`` are stored as plain float32 / NULL today and
        the dashboard displays 4 decimal places). If finer accounting
        ever becomes a concern (sub-cent billing across millions of
        calls), this returns to the conversation in #474.
        """
        if units <= 0:
            return 0.0

        row = await self.lookup(
            provider=provider,
            model=model,
            unit_type=unit_type,
            started_at=started_at,
            context_tokens=context_tokens,
        )
        if row is None:
            return None

        return float(units) * float(row.price_per_unit) / float(row.unit_denominator)
