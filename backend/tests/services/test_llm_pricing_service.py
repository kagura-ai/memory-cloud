"""Unit tests for ``LLMPricingService``'s process-local price cache (#713).

The recall hot path resolves the same ``(provider, model, embedding_tokens)``
price twice per call (spend cap + cost ledger). ``_cached_price_components``
collapses that to a single ``lookup()`` round-trip with a 60-minute TTL. These
tests mock ``lookup`` directly so they assert the caching contract without a DB.

Cross-test isolation is provided by the autouse ``_clear_pricing_cache`` fixture
in ``tests/conftest.py`` — every test starts from an empty cache.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.llm_pricing_service import LLMPricingService, clear_pricing_cache

_STARTED_AT = datetime(2026, 5, 27, 12, 0, 0)


@pytest.fixture(autouse=True)
def _isolate_pricing_cache():
    """Clear the module-global price cache around every test in this file.

    Every assertion here is on ``lookup`` call-count, which is meaningless
    unless the cache starts empty. The root ``conftest`` also clears it, but
    this file's correctness depends on the clear directly, so it is pinned
    locally too — the redundancy is a free ``dict.clear()`` and keeps the
    call-count tests honest even if the root fixture is ever removed.
    """
    clear_pricing_cache()
    yield
    clear_pricing_cache()


def _service() -> LLMPricingService:
    """Service with a dummy session — ``lookup`` is mocked, DB never touched."""
    return LLMPricingService(MagicMock())


def _pricing_row(price_per_unit: str, unit_denominator: int) -> MagicMock:
    row = MagicMock()
    row.price_per_unit = Decimal(price_per_unit)
    row.unit_denominator = unit_denominator
    return row


@pytest.mark.asyncio
async def test_repeated_calls_hit_cache_single_lookup():
    """100 ``compute_cost_usd`` calls with the same key → ``lookup`` called once."""
    service = _service()
    service.lookup = AsyncMock(return_value=_pricing_row("0.02", 1_000_000))

    costs = [
        await service.compute_cost_usd(
            provider="openai",
            model="text-embedding-3-small",
            unit_type="embedding_tokens",
            started_at=_STARTED_AT,
            units=1_000_000,
        )
        for _ in range(100)
    ]

    assert service.lookup.await_count == 1
    # Result still computed correctly per-call (units * price / denominator).
    assert all(c == pytest.approx(0.02) for c in costs)


@pytest.mark.asyncio
async def test_units_scale_per_call_despite_shared_cache():
    """The cached value is the price row, not the cost — units still vary."""
    service = _service()
    service.lookup = AsyncMock(return_value=_pricing_row("0.02", 1_000_000))

    cost_1m = await service.compute_cost_usd(
        provider="openai",
        model="text-embedding-3-small",
        unit_type="embedding_tokens",
        started_at=_STARTED_AT,
        units=1_000_000,
    )
    cost_2m = await service.compute_cost_usd(
        provider="openai",
        model="text-embedding-3-small",
        unit_type="embedding_tokens",
        started_at=_STARTED_AT,
        units=2_000_000,
    )

    assert cost_1m == pytest.approx(0.02)
    assert cost_2m == pytest.approx(0.04)
    assert service.lookup.await_count == 1


@pytest.mark.asyncio
async def test_distinct_keys_each_trigger_lookup():
    """Different (provider, model, unit_type, date) keys are cached separately."""
    service = _service()
    service.lookup = AsyncMock(return_value=_pricing_row("0.02", 1_000_000))

    base = {"unit_type": "embedding_tokens", "started_at": _STARTED_AT, "units": 100}
    await service.compute_cost_usd(provider="openai", model="m-a", **base)
    await service.compute_cost_usd(provider="openai", model="m-b", **base)
    await service.compute_cost_usd(provider="voyage", model="m-a", **base)
    await service.compute_cost_usd(
        provider="openai",
        model="m-a",
        unit_type="embedding_tokens",
        started_at=datetime(2026, 5, 28, 12, 0, 0),  # different date
        units=100,
    )

    assert service.lookup.await_count == 4


@pytest.mark.asyncio
async def test_lookup_miss_is_negatively_cached():
    """A miss (``None``) is cached so an unseeded model doesn't re-query."""
    service = _service()
    service.lookup = AsyncMock(return_value=None)

    results = [
        await service.compute_cost_usd(
            provider="openai",
            model="brand-new-model",
            unit_type="embedding_tokens",
            started_at=_STARTED_AT,
            units=100,
        )
        for _ in range(5)
    ]

    assert all(r is None for r in results)
    assert service.lookup.await_count == 1


@pytest.mark.asyncio
async def test_clear_pricing_cache_forces_relookup():
    """``clear_pricing_cache()`` drops entries so the next call re-queries."""
    service = _service()
    service.lookup = AsyncMock(return_value=_pricing_row("0.02", 1_000_000))

    call = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "unit_type": "embedding_tokens",
        "started_at": _STARTED_AT,
        "units": 100,
    }
    await service.compute_cost_usd(**call)
    assert service.lookup.await_count == 1

    clear_pricing_cache()

    await service.compute_cost_usd(**call)
    assert service.lookup.await_count == 2


@pytest.mark.asyncio
async def test_zero_units_short_circuits_before_lookup():
    """``units == 0`` returns 0.0 without consulting the cache or DB."""
    service = _service()
    service.lookup = AsyncMock()

    result = await service.compute_cost_usd(
        provider="openai",
        model="text-embedding-3-small",
        unit_type="embedding_tokens",
        started_at=_STARTED_AT,
        units=0,
    )

    assert result == 0.0
    service.lookup.assert_not_awaited()
