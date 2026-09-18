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

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from cachetools import TTLCache
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_pricing import LLM_PRICING_UNIT_TYPES, LLMPricing
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.llm_pricing_overrides import PricingOverride

logger = get_logger(__name__)

# Process-local price cache (Issue #713). Pricing rows in ``llm_pricing`` are
# read-mostly — they change at most a few times per year — yet the recall hot
# path resolves the same ``(provider, model, embedding_tokens)`` price TWICE per
# call: once in ``EmbeddingSpendCapService.record_spend_from_tokens`` for the
# BYOK spend cap, once in ``LLMCallLogWriter.record`` for the cost ledger. A
# small in-process TTL cache collapses both to a single SELECT and shares the
# result across every request in the worker, since pricing is global, not
# tenant-scoped. Process-local (not Redis) is deliberate — the value it replaces
# is itself one indexed SELECT, so a Redis round-trip would not beat it; the
# issue records Redis as out of scope for the same reason. The 60-minute TTL
# bounds post-change staleness to one hour (acceptable per the issue) at the
# cost of up-to-1h cross-worker skew after a price change; explicit invalidation
# is therefore unnecessary for v1, but ``clear_pricing_cache()`` is exposed for
# tests and post-seed refresh. Misses are cached too (value ``None``) so an
# unseeded model doesn't re-query.
_PRICING_CACHE_TTL_SECONDS: Final = 3600
_PRICING_CACHE_MAXSIZE: Final = 1024
# key: (provider, model, unit_type, started_at.date(), context_tokens)
# value: (price_per_unit, unit_denominator) | None
_pricing_cache: TTLCache[tuple[str, str, str, date, int], tuple[float, float] | None] = TTLCache[
    tuple[str, str, str, date, int], tuple[float, float] | None
](maxsize=_PRICING_CACHE_MAXSIZE, ttl=_PRICING_CACHE_TTL_SECONDS)


def clear_pricing_cache() -> None:
    """Drop all entries from the process-local pricing cache.

    Exposed for two callers: tests that need a deterministic cache state,
    and ops tooling that wants newly-seeded ``llm_pricing`` rows to take
    effect immediately rather than after the 60-minute TTL.
    """
    _pricing_cache.clear()


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
                ``'openai'``, ``'self_hosted'``).
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
        if units < 0:
            raise ValueError(f"units must be >= 0, got {units}")
        # Validate ``unit_type`` BEFORE the ``units == 0`` fast-path so a
        # typo gets the same ValueError it would in ``lookup()``. Otherwise
        # ``compute_cost_usd(unit_type='input_tokens_typo', units=0)`` would
        # silently return 0.0 and hide the bug until the next non-zero call.
        if unit_type not in LLM_PRICING_UNIT_TYPES:
            raise ValueError(
                f"Invalid unit_type {unit_type!r}; must be one of {LLM_PRICING_UNIT_TYPES}"
            )
        if units == 0:
            return 0.0

        components = await self._cached_price_components(
            provider=provider,
            model=model,
            unit_type=unit_type,
            started_at=started_at,
            context_tokens=context_tokens,
        )
        if components is None:
            return None

        price_per_unit, unit_denominator = components
        return float(units) * price_per_unit / unit_denominator

    async def has_positive_price(
        self,
        *,
        provider: str,
        model: str,
        unit_type: str,
        started_at: datetime,
        context_tokens: int = 0,
    ) -> bool:
        """Is there an effective price row with ``price_per_unit > 0``? (#1570)

        ``False`` both for a lookup miss and for an explicit ``$0`` row — the
        two are indistinguishable for the callers that ask (a ``$0`` model has
        nothing to cap either). Goes through the process-local cache, so the
        recall hot path pays at most one SELECT per hour per model.
        """
        components = await self._cached_price_components(
            provider=provider,
            model=model,
            unit_type=unit_type,
            started_at=started_at,
            context_tokens=context_tokens,
        )
        return components is not None and components[0] > 0

    async def _cached_price_components(
        self,
        *,
        provider: str,
        model: str,
        unit_type: str,
        started_at: datetime,
        context_tokens: int,
    ) -> tuple[float, float] | None:
        """Return ``(price_per_unit, unit_denominator)`` for a call, cached (#713).

        Wraps ``lookup()`` with the process-local ``_pricing_cache`` so the
        duplicate per-recall pricing SELECT collapses to a single round-trip.
        Returns ``None`` for a lookup miss (caller writes ``cost_usd = NULL``),
        and that ``None`` is cached so an unseeded model doesn't re-query every
        call.

        The cache key keeps ``context_tokens`` so multi-tier models (e.g. Gemini
        2.5 Pro's 200k breakpoint) still resolve to the correct tier — the AC's
        ``(provider, model, unit_type, date)`` key is a subset of this one.

        **Snapshot axis = ``started_at.date()`` (day granularity), deliberately.**
        The two hot-path lookups for one recall
        (``EmbeddingSpendCapService.record_spend_from_tokens`` and
        ``LLMCallLogWriter.record``) each call ``utcnow()`` independently, so they
        carry timestamps milliseconds apart; day-bucketing is what collapses them
        into the single SELECT this cache exists to save. A finer key (e.g. full
        ``started_at``) would never hit between those two calls and would defeat
        the optimization.

        Reproducibility bound this trades away: ``lookup()`` documents
        ``effective_from <= started_at`` for *exact* historical reproducibility,
        but ``effective_from`` is an unconstrained ``DateTime`` (it MAY be
        intra-day). Day-bucketing therefore guarantees reproducibility only at
        **day granularity** — two calls on the same UTC date that straddle an
        intra-day ``effective_from`` change collapse to whichever price was cached
        first. This is bounded and acceptable today because: (a) on the live path
        ``started_at`` is always ``utcnow()`` and the 60-minute TTL already caps
        staleness to ≤1h regardless of key granularity; (b) the historical-replay
        path (``record_spend_from_tokens(occurred_at=...)``) has no live caller.
        **If sub-day-precise historical replay is ever wired up, this key needs a
        finer time component OR the replay must bypass the cache** (Copilot review
        on PR #811).
        """
        cache_key = (provider, model, unit_type, started_at.date(), context_tokens)
        # A single ``__getitem__`` reads the TTL clock once, so it is race-free
        # (unlike ``key in cache`` then ``cache[key]``, which read the clock
        # twice and can raise on an entry expiring between them). A stored ``None``
        # (negative cache) returns ``None`` here; only an absent OR expired key
        # raises ``KeyError``, both of which mean "re-query". Typed value, so a
        # wrong-typed cache write is still caught by the checker.
        try:
            return _pricing_cache[cache_key]
        except KeyError:
            pass

        row = await self.lookup(
            provider=provider,
            model=model,
            unit_type=unit_type,
            started_at=started_at,
            context_tokens=context_tokens,
        )
        components = (
            None if row is None else (float(row.price_per_unit), float(row.unit_denominator))
        )
        _pricing_cache[cache_key] = components
        return components


# ---------------------------------------------------------------------------
# Operator overrides → table (#1570)
# ---------------------------------------------------------------------------

# ``llm_pricing.price_per_unit`` is ``Numeric(14, 10)``; compare at the column's
# scale so an override with more decimals than the column keeps does not read
# back as "different" and re-insert on every boot.
_PRICE_SCALE: Final = Decimal("1e-10")


@dataclass
class SyncResult:
    """Outcome of one ``sync_llm_pricing_overrides`` pass."""

    inserted: list[PricingOverride] = field(default_factory=list)
    unchanged: list[PricingOverride] = field(default_factory=list)


async def _effective_row(
    db: AsyncSession, override: PricingOverride, *, now: datetime
) -> LLMPricing | None:
    """The row ``lookup()`` would pick for this override's exact tier at ``now``."""
    stmt = (
        select(LLMPricing)
        .where(
            and_(
                LLMPricing.provider == override.provider,
                LLMPricing.model == override.model,
                LLMPricing.unit_type == override.unit_type,
                LLMPricing.context_min_tokens == override.context_min_tokens,
                LLMPricing.effective_from <= now,
            )
        )
        .order_by(LLMPricing.effective_from.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def sync_llm_pricing_overrides(
    db: AsyncSession,
    overrides: Iterable[PricingOverride],
    *,
    now: datetime,
    dry_run: bool = False,
) -> SyncResult:
    """Materialize ``LLM_PRICING_OVERRIDES`` into ``llm_pricing`` (#1570).

    For each override, find the row currently effective for the same
    ``(provider, model, unit_type, context_min_tokens)``. When there is none,
    or its ``(price_per_unit, unit_denominator)`` differ, INSERT a new row
    with ``effective_from=now`` (``pricing_model='per_token'``, USD); otherwise
    do nothing. Existing rows are never updated or deleted — the table is an
    append-only snapshot history and ``lookup()`` picks the newest row whose
    ``effective_from <= started_at``, so the new price takes over from ``now``
    while historical reports keep the rate that applied at the time.

    The table is the single source of truth for ``LLMPricingService``, the SQL
    LATERAL joins in ``cost_aggregation_service`` and the
    ``memory_analyses.model_id`` FK — an in-memory override layer would miss
    the SQL aggregation, which is why the env value is written down here.

    Flushes but does not commit; the caller owns the transaction. Clears the
    process-local price cache after any insert so the new price is visible
    without waiting out the TTL. Multiple replicas syncing at the same moment
    may each insert an identical-price row with a slightly different
    ``effective_from`` — harmless (newest wins, same price); two inserts in the
    very same microsecond hit the unique constraint and the caller's error
    path.

    Args:
        db: Async session; the caller commits.
        overrides: Parsed ``LLM_PRICING_OVERRIDES`` entries.
        now: Naive UTC timestamp used as ``effective_from`` for inserts (and
            as the "currently effective" cut-off).
        dry_run: Compute the diff but write nothing (the CLI's ``--plan``).
            ``inserted`` then lists what a real run would append.

    Returns:
        ``SyncResult`` listing the overrides that were inserted and those
        that already matched the effective row.
    """
    result = SyncResult()
    for override in overrides:
        current = await _effective_row(db, override, now=now)
        wanted_price = override.price_per_unit.quantize(_PRICE_SCALE)
        if (
            current is not None
            and Decimal(current.price_per_unit) == wanted_price
            and int(current.unit_denominator) == override.unit_denominator
        ):
            result.unchanged.append(override)
            continue
        result.inserted.append(override)
        if dry_run:
            continue
        db.add(
            LLMPricing(
                provider=override.provider,
                model=override.model,
                unit_type=override.unit_type,
                effective_from=now,
                context_min_tokens=override.context_min_tokens,
                pricing_model="per_token",
                price_per_unit=override.price_per_unit,
                currency="USD",
                unit_denominator=override.unit_denominator,
            )
        )
        logger.info(
            "llm_pricing_override_applied",
            provider=override.provider,
            model=override.model,
            unit_type=override.unit_type,
            price_per_unit=str(override.price_per_unit),
            unit_denominator=override.unit_denominator,
            context_min_tokens=override.context_min_tokens,
            previous_price=None if current is None else str(current.price_per_unit),
        )
    if result.inserted and not dry_run:
        await db.flush()
        clear_pricing_cache()
    logger.info(
        "llm_pricing_overrides_synced",
        inserted=len(result.inserted),
        unchanged=len(result.unchanged),
        dry_run=dry_run,
    )
    return result
