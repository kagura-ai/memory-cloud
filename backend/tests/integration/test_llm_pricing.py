"""Integration tests for the cost-grade pricing schema (Issue #471).

Covers:

- ``LLMPricingService.lookup()`` — temporal validity (``effective_from``),
  context tier breakpoints, ``unit_type`` selection, and the documented
  miss-returns-None behavior.
- ``LLMPricingService.compute_cost_usd()`` — math correctness across
  per-1M-tokens (``unit_denominator=1000000``) and per-1k-search-units
  (``unit_denominator=1000``) rows.
- CHECK constraints on ``llm_pricing`` — invalid ``unit_type``,
  negative ``price_per_unit``, zero ``unit_denominator``, and the tier
  ``context_max_tokens > context_min_tokens`` invariant.
- Schema-supports-rerank-but-no-rows: a lookup for
  ``unit_type='rerank_search_units'`` must return ``None`` cleanly
  (#471 reserves the enum value for #474).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from models.llm_pricing import LLMPricing
from services.llm_pricing_service import LLMPricingService


def _make_row(
    *,
    provider: str,
    model: str,
    unit_type: str,
    effective_from: datetime,
    price_per_unit: str,
    context_min_tokens: int = 0,
    context_max_tokens: int | None = None,
    unit_denominator: int = 1_000_000,
    currency: str = "USD",
) -> LLMPricing:
    """Test-helper constructor for ``LLMPricing`` rows."""
    return LLMPricing(
        provider=provider,
        model=model,
        unit_type=unit_type,
        effective_from=effective_from,
        price_per_unit=price_per_unit,
        context_min_tokens=context_min_tokens,
        context_max_tokens=context_max_tokens,
        unit_denominator=unit_denominator,
        currency=currency,
    )


@pytest.mark.asyncio
async def test_lookup_picks_most_recent_effective_from(db_session):
    """A re-priced model returns the snapshot active at started_at."""
    db_session.add_all(
        [
            _make_row(
                provider="anthropic",
                model="claude-sonnet-4-6",
                unit_type="input_tokens",
                effective_from=datetime(
                    2026,
                    1,
                    1,
                ),
                price_per_unit="2.50",
            ),
            _make_row(
                provider="anthropic",
                model="claude-sonnet-4-6",
                unit_type="input_tokens",
                effective_from=datetime(
                    2026,
                    4,
                    1,
                ),
                price_per_unit="3.00",
            ),
        ]
    )
    await db_session.flush()

    svc = LLMPricingService(db_session)

    # Run started before the rate change → old rate.
    early = await svc.lookup(
        provider="anthropic",
        model="claude-sonnet-4-6",
        unit_type="input_tokens",
        started_at=datetime(
            2026,
            2,
            15,
        ),
    )
    assert early is not None
    assert float(early.price_per_unit) == 2.50

    # Run started after the change → new rate.
    late = await svc.lookup(
        provider="anthropic",
        model="claude-sonnet-4-6",
        unit_type="input_tokens",
        started_at=datetime(
            2026,
            4,
            28,
        ),
    )
    assert late is not None
    assert float(late.price_per_unit) == 3.00


@pytest.mark.asyncio
async def test_lookup_picks_correct_context_tier(db_session):
    """Gemini-shaped multi-tier model: lookup picks the matching tier row."""
    effective = datetime(
        2026,
        4,
        28,
    )
    db_session.add_all(
        [
            _make_row(
                provider="google",
                model="gemini-2.5-pro",
                unit_type="input_tokens",
                effective_from=effective,
                price_per_unit="1.25",
                context_min_tokens=0,
                context_max_tokens=200_000,
            ),
            _make_row(
                provider="google",
                model="gemini-2.5-pro",
                unit_type="input_tokens",
                effective_from=effective,
                price_per_unit="2.50",
                context_min_tokens=200_000,
                context_max_tokens=None,
            ),
        ]
    )
    await db_session.flush()

    svc = LLMPricingService(db_session)

    low = await svc.lookup(
        provider="google",
        model="gemini-2.5-pro",
        unit_type="input_tokens",
        started_at=effective,
        context_tokens=50_000,
    )
    assert low is not None
    assert float(low.price_per_unit) == 1.25

    high = await svc.lookup(
        provider="google",
        model="gemini-2.5-pro",
        unit_type="input_tokens",
        started_at=effective,
        context_tokens=300_000,
    )
    assert high is not None
    assert float(high.price_per_unit) == 2.50


@pytest.mark.asyncio
async def test_lookup_miss_returns_none(db_session):
    """Lookup for an unseeded model returns None — caller writes cost_usd=NULL."""
    svc = LLMPricingService(db_session)
    result = await svc.lookup(
        provider="exotic",
        model="never-seeded",
        unit_type="input_tokens",
        started_at=datetime(
            2026,
            4,
            28,
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_lookup_rerank_search_units_schema_supports_no_rows(db_session):
    """The rerank_search_units enum exists in #471 but no rows are seeded.

    This verifies the schema accepts the enum value without breaking
    token-based queries — #474 will seed actual rerank rows. The lookup
    must return ``None`` cleanly, not raise.
    """
    svc = LLMPricingService(db_session)
    result = await svc.lookup(
        provider="cohere",
        model="rerank-3.5",
        unit_type="rerank_search_units",
        started_at=datetime(
            2026,
            4,
            28,
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_lookup_invalid_unit_type_raises(db_session):
    """Service-layer validation rejects typos before they hit the DB."""
    svc = LLMPricingService(db_session)
    with pytest.raises(ValueError, match="Invalid unit_type"):
        await svc.lookup(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="not_a_real_unit_type",  # type: ignore[arg-type]
            started_at=datetime(
                2026,
                4,
                28,
            ),
        )


@pytest.mark.asyncio
async def test_compute_cost_usd_per_million_tokens(db_session):
    """1M tokens at $3/1M → $3.00; 100k tokens → $0.30.

    Uses ``effective_from=2026-04-27`` (one day before the seed
    migration's ``2026-04-28`` row) so the INSERT does not collide
    with the seeded ``(anthropic, claude-sonnet-4-6, input_tokens,
    2026-04-28)`` row on ``uq_llm_pricing_lookup_key`` when the
    integration suite runs ``test_alembic_migrations`` before this
    test. The lookup math is unchanged because ``$3/1M`` matches
    the seeded rate; whichever row the temporal selector picks
    produces the same expected cost.
    """
    insert_effective = datetime(2026, 4, 27)
    db_session.add(
        _make_row(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="input_tokens",
            effective_from=insert_effective,
            price_per_unit="3.00",
        )
    )
    await db_session.flush()

    svc = LLMPricingService(db_session)
    cost = await svc.compute_cost_usd(
        provider="anthropic",
        model="claude-sonnet-4-6",
        unit_type="input_tokens",
        started_at=insert_effective,
        units=100_000,
    )
    assert cost is not None
    assert cost == pytest.approx(0.30)

    # 1M tokens → exactly $3.
    cost_full = await svc.compute_cost_usd(
        provider="anthropic",
        model="claude-sonnet-4-6",
        unit_type="input_tokens",
        started_at=insert_effective,
        units=1_000_000,
    )
    assert cost_full == pytest.approx(3.00)


@pytest.mark.asyncio
async def test_compute_cost_usd_per_thousand_search_units(db_session):
    """Cohere-shaped per-1k-SU pricing: 500 search units at $2/1k → $1.00."""
    db_session.add(
        _make_row(
            provider="cohere",
            model="rerank-3.5",
            unit_type="rerank_search_units",
            effective_from=datetime(
                2026,
                4,
                28,
            ),
            price_per_unit="2.00",
            unit_denominator=1_000,
        )
    )
    await db_session.flush()

    svc = LLMPricingService(db_session)
    cost = await svc.compute_cost_usd(
        provider="cohere",
        model="rerank-3.5",
        unit_type="rerank_search_units",
        started_at=datetime(
            2026,
            4,
            28,
        ),
        units=500,
    )
    assert cost == pytest.approx(1.00)


@pytest.mark.asyncio
async def test_compute_cost_usd_zero_units(db_session):
    """Zero units → $0.00 without hitting the DB."""
    svc = LLMPricingService(db_session)
    cost = await svc.compute_cost_usd(
        provider="ollama",
        model="never-seeded",
        unit_type="embedding_tokens",
        started_at=datetime(
            2026,
            4,
            28,
        ),
        units=0,
    )
    assert cost == 0.0


@pytest.mark.asyncio
async def test_compute_cost_usd_lookup_miss_returns_none(db_session):
    """Lookup miss propagates as None (caller writes cost_usd=NULL)."""
    svc = LLMPricingService(db_session)
    cost = await svc.compute_cost_usd(
        provider="exotic",
        model="never-seeded",
        unit_type="output_tokens",
        started_at=datetime(
            2026,
            4,
            28,
        ),
        units=1000,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_check_constraint_invalid_unit_type(db_session):
    """DB CHECK rejects unknown unit_type values (defense in depth)."""
    db_session.add(
        LLMPricing(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="bogus_type",
            effective_from=datetime(
                2026,
                4,
                28,
            ),
            price_per_unit="1.00",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_constraint_negative_price(db_session):
    """DB CHECK rejects negative prices."""
    db_session.add(
        LLMPricing(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="input_tokens",
            effective_from=datetime(
                2026,
                4,
                28,
            ),
            price_per_unit="-1.00",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_check_constraint_zero_unit_denominator(db_session):
    """DB CHECK rejects unit_denominator=0 (would produce divide-by-zero in cost math).

    Uses ``effective_from=2026-04-27`` (off the seed migration's
    ``2026-04-28`` row) so the IntegrityError raised on flush is the
    expected ``unit_denominator=0`` CHECK violation, not a coincidental
    ``uq_llm_pricing_lookup_key`` UNIQUE violation against the seed.
    """
    db_session.add(
        LLMPricing(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="input_tokens",
            effective_from=datetime(2026, 4, 27),
            price_per_unit="3.00",
            unit_denominator=0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_compute_cost_usd_negative_units_raises(db_session):
    """Negative ``units`` should raise ValueError (not silently return 0).

    Hides upstream bugs and silently under-reports cost. The fast-path for
    ``units == 0`` returning 0.0 is preserved.
    """
    svc = LLMPricingService(db_session)
    with pytest.raises(ValueError, match="units must be >= 0"):
        await svc.compute_cost_usd(
            provider="anthropic",
            model="claude-sonnet-4-6",
            unit_type="input_tokens",
            started_at=datetime(2026, 4, 28),
            units=-1,
        )


@pytest.mark.asyncio
async def test_check_constraint_tier_max_must_exceed_min(db_session):
    """DB CHECK rejects context_max <= context_min."""
    db_session.add(
        LLMPricing(
            provider="google",
            model="gemini-2.5-pro",
            unit_type="input_tokens",
            effective_from=datetime(
                2026,
                4,
                28,
            ),
            price_per_unit="1.25",
            context_min_tokens=200_000,
            context_max_tokens=200_000,  # equal — must be strictly greater
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
