"""#1570: ``LLM_PRICING_OVERRIDES`` → ``llm_pricing`` sync, and what it unlocks.

Real Postgres (``db_session``). Pins the issue's acceptance:

- sync inserts the row; a second run inserts nothing; a changed price appends a
  new row (the old one stays — append-only history);
- a priced ``self_hosted`` embedding model reports non-null, non-zero cost in
  the SQL cost aggregation; an unpriced one aggregates to NULL.

Each test prices its own uniquely-named ``self_hosted`` model so the assertions
do not depend on (or disturb) the seed rows / rows other tests reference.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from config.llm_pricing_overrides import PricingOverride
from models.llm_pricing import LLMPricing
from models.sleep import SleepReport
from services.cost_aggregation_service import CostAggregationService
from services.llm_pricing_service import (
    LLMPricingService,
    clear_pricing_cache,
    sync_llm_pricing_overrides,
)

_T1 = datetime(2026, 1, 1, 0, 0, 0)
_T2 = datetime(2026, 2, 1, 0, 0, 0)
_T3 = datetime(2026, 3, 1, 0, 0, 0)


@pytest.fixture
def model() -> str:
    """A self_hosted embedding model name no other test (or seed) prices."""
    return f"qwen3-embedding:4b-{uuid4().hex[:8]}"


@pytest_asyncio.fixture(autouse=True)
async def _isolate_pricing_cache():
    clear_pricing_cache()
    yield
    clear_pricing_cache()


def _override(model: str, price: str, **kwargs) -> PricingOverride:
    return PricingOverride(
        provider="self_hosted",
        model=model,
        unit_type="embedding_tokens",
        price_per_unit=Decimal(price),
        **kwargs,
    )


async def _rows(db_session, model: str) -> list[LLMPricing]:
    stmt = (
        select(LLMPricing)
        .where(
            LLMPricing.provider == "self_hosted",
            LLMPricing.model == model,
            LLMPricing.unit_type == "embedding_tokens",
        )
        .order_by(LLMPricing.effective_from)
    )
    return list((await db_session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_sync_inserts_once_and_appends_on_price_change(db_session, model):
    # First run: the row does not exist → inserted.
    result = await sync_llm_pricing_overrides(db_session, [_override(model, "0.02")], now=_T1)
    assert [o.model for o in result.inserted] == [model]
    assert result.unchanged == []
    (row,) = await _rows(db_session, model)
    assert (row.price_per_unit, row.unit_denominator) == (Decimal("0.02"), 1_000_000)
    assert (row.effective_from, row.pricing_model, row.currency) == (_T1, "per_token", "USD")

    # The price service now resolves it: positive → cappable, and the math is right.
    pricing = LLMPricingService(db_session)
    assert await pricing.has_positive_price(
        provider="self_hosted", model=model, unit_type="embedding_tokens", started_at=_T2
    )
    cost = await pricing.compute_cost_usd(
        provider="self_hosted",
        model=model,
        unit_type="embedding_tokens",
        started_at=_T2,
        units=1_000_000,
    )
    assert cost == pytest.approx(0.02)

    # Second run, same price → idempotent.
    result = await sync_llm_pricing_overrides(db_session, [_override(model, "0.02")], now=_T2)
    assert result.inserted == []
    assert [o.model for o in result.unchanged] == [model]
    assert len(await _rows(db_session, model)) == 1

    # Changed price → a NEW row from now; the old one stays for history.
    result = await sync_llm_pricing_overrides(db_session, [_override(model, "0.05")], now=_T3)
    assert [o.price_per_unit for o in result.inserted] == [Decimal("0.05")]
    rows = await _rows(db_session, model)
    assert [(r.effective_from, r.price_per_unit) for r in rows] == [
        (_T1, Decimal("0.02")),
        (_T3, Decimal("0.05")),
    ]
    clear_pricing_cache()
    new_price = await pricing.lookup(
        provider="self_hosted", model=model, unit_type="embedding_tokens", started_at=_T3
    )
    old_price = await pricing.lookup(
        provider="self_hosted", model=model, unit_type="embedding_tokens", started_at=_T2
    )
    assert new_price is not None and new_price.price_per_unit == Decimal("0.05")
    assert old_price is not None and old_price.price_per_unit == Decimal("0.02")


@pytest.mark.asyncio
async def test_sync_treats_denominator_change_as_a_new_price(db_session, model):
    await sync_llm_pricing_overrides(db_session, [_override(model, "0.02")], now=_T1)
    result = await sync_llm_pricing_overrides(
        db_session, [_override(model, "0.02", unit_denominator=1_000)], now=_T2
    )
    assert len(result.inserted) == 1
    assert len(await _rows(db_session, model)) == 2


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing(db_session, model):
    result = await sync_llm_pricing_overrides(
        db_session, [_override(model, "0.02")], now=_T1, dry_run=True
    )
    assert [o.model for o in result.inserted] == [model]
    assert await _rows(db_session, model) == []


@pytest.mark.asyncio
async def test_empty_overrides_touch_nothing(db_session):
    before = (await db_session.execute(select(func.count()).select_from(LLMPricing))).scalar_one()
    result = await sync_llm_pricing_overrides(db_session, [], now=_T1)
    after = (await db_session.execute(select(func.count()).select_from(LLMPricing))).scalar_one()
    assert (result.inserted, result.unchanged) == ([], [])
    assert before == after


@pytest.mark.asyncio
async def test_cost_aggregation_prices_self_hosted_only_when_overridden(db_session, model):
    """Acceptance: priced self_hosted → non-null, non-zero cost; unpriced → NULL."""
    await sync_llm_pricing_overrides(db_session, [_override(model, "0.02")], now=_T1)
    unpriced_model = f"unpriced-{uuid4().hex[:8]}"

    priced_ws, unpriced_ws = uuid4(), uuid4()
    for workspace_id, embedding_model in ((priced_ws, model), (unpriced_ws, unpriced_model)):
        db_session.add(
            SleepReport(
                user_id=f"user-{workspace_id.hex[:8]}",
                workspace_id=workspace_id,
                status="completed",
                started_at=datetime(2026, 4, 5, 3, 0),
                source="sleep",
                paid_by="platform",
                embedding_tokens=10_000_000,  # 10M × $0.02/1M = $0.20
                embedding_provider="self_hosted",
                embedding_model=embedding_model,
            )
        )
    await db_session.flush()

    service = CostAggregationService(db_session)
    (priced,) = await service.aggregate(
        period="day", start=datetime(2026, 4, 1), end=datetime(2026, 4, 8), workspace_id=priced_ws
    )
    (unpriced,) = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=unpriced_ws,
    )
    assert priced.embedding_tokens == 10_000_000
    assert priced.cost_usd == pytest.approx(0.20)
    assert unpriced.embedding_tokens == 10_000_000
    assert unpriced.cost_usd is None
