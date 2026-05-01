"""Integration tests for ``CostAggregationService`` (Issue #472).

Exercises the SQL pipeline against a real Postgres DB so the LATERAL
joins, ``date_trunc``, ``FILTER (WHERE ...)``, and the multi-CTE
``UNION ALL`` are all proven end-to-end. Mocked-DB unit tests would
silently pass with the wrong SQL because asyncpg never sees the query.

Coverage:

- empty period → no rows returned
- multi-model rows roll into one ``cost_breakdown_by_model`` per
  ``(period, workspace, user)``
- BYOK rows go into ``cost_usd_byok`` and never contribute to ``cost_usd``
- ``source='analysis'`` rows surface in ``cost_breakdown_by_source``
- pricing miss → row still surfaces (cost = 0), usage figures preserved
- filter combinations: ``source=analysis & paid_by=byok`` returns the
  intersection only
- input validation: bad period / source / paid_by / inverted window
  raises ``ValueError`` (the route layer maps to 400)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from models.llm_pricing import LLMPricing
from models.sleep import SleepReport, SleepReportLLMUsage
from services.cost_aggregation_service import CostAggregationService

# All pricing rows in this module share one effective_from that pre-dates
# every test report's ``started_at``. Keeps the LATERAL pricing lookup
# deterministic across tests.
_PRICE_EFFECTIVE = datetime(2026, 1, 1, 0, 0, 0)


async def _seed_pricing(db_session) -> None:
    """Seed minimal pricing rows the SQL pipeline needs to compute cost.

    Per-1M tokens for clarity (matches production seed convention).
    Only the dimensions used by the tests are seeded — keeps each test
    cheap and the failing-query post-mortem readable.
    """
    rows = [
        # claude-sonnet-4-6: input $3 / output $15 / cache_read $0.30 per 1M
        ("anthropic", "claude-sonnet-4-6", "input_tokens", "3"),
        ("anthropic", "claude-sonnet-4-6", "output_tokens", "15"),
        ("anthropic", "claude-sonnet-4-6", "cache_read_tokens", "0.30"),
        # claude-haiku-4-5: input $1 / output $5 / cache_read $0.10 per 1M
        ("anthropic", "claude-haiku-4-5", "input_tokens", "1"),
        ("anthropic", "claude-haiku-4-5", "output_tokens", "5"),
        ("anthropic", "claude-haiku-4-5", "cache_read_tokens", "0.10"),
        # text-embedding-3-small: $0.02 per 1M
        ("openai", "text-embedding-3-small", "embedding_tokens", "0.02"),
    ]
    for provider, model, unit_type, price in rows:
        db_session.add(
            LLMPricing(
                provider=provider,
                model=model,
                unit_type=unit_type,
                effective_from=_PRICE_EFFECTIVE,
                context_min_tokens=0,
                price_per_unit=Decimal(price),
                unit_denominator=1_000_000,
            )
        )
    await db_session.flush()


def _make_report(
    *,
    user_id: str,
    workspace_id: UUID,
    started_at: datetime,
    source: str = "sleep",
    paid_by: str = "platform",
    embedding_tokens: int = 0,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> SleepReport:
    """Construct a SleepReport for a given period bucket. status='completed'."""
    return SleepReport(
        user_id=user_id,
        workspace_id=workspace_id,
        status="completed",
        started_at=started_at,
        source=source,
        paid_by=paid_by,
        embedding_tokens=embedding_tokens,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )


def _make_usage(
    *,
    report_id: UUID,
    provider: str,
    model: str,
    calls: int,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    phase: str = "edge_discovery",
) -> SleepReportLLMUsage:
    """Construct a ``sleep_report_llm_usage`` child row."""
    return SleepReportLLMUsage(
        report_id=report_id,
        phase=phase,
        provider=provider,
        model=model,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )


@pytest.mark.asyncio
async def test_empty_period_returns_empty_list(db_session):
    """A period with no sleep_reports rows returns an empty list."""
    await _seed_pricing(db_session)

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
    )
    assert rows == []


@pytest.mark.asyncio
async def test_multi_model_rolls_into_breakdown_by_model(db_session):
    """Two models in the same period bucket each emit a breakdown_by_model entry.

    Sonnet 100 calls × (10k in @ $3/1M + 2k out @ $15/1M) = $0.06
    Haiku 50 calls × (8k in @ $1/1M + 1k out @ $5/1M) = $0.013
    """
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-multi-model"

    sonnet_report = _make_report(
        user_id=user_id, workspace_id=workspace_id, started_at=datetime(2026, 4, 5, 3, 0)
    )
    haiku_report = _make_report(
        user_id=user_id, workspace_id=workspace_id, started_at=datetime(2026, 4, 5, 4, 0)
    )
    db_session.add_all([sonnet_report, haiku_report])
    await db_session.flush()

    db_session.add_all(
        [
            _make_usage(
                report_id=sonnet_report.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=100,
                input_tokens=10_000,
                output_tokens=2_000,
            ),
            _make_usage(
                report_id=haiku_report.id,
                provider="anthropic",
                model="claude-haiku-4-5",
                calls=50,
                input_tokens=8_000,
                output_tokens=1_000,
            ),
        ]
    )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]

    assert row.period_start == date(2026, 4, 5)
    assert row.workspace_id == workspace_id
    assert row.user_id == user_id
    assert row.calls == 150
    assert row.tokens_in == 18_000
    assert row.tokens_out == 3_000

    by_model = {b.model: b for b in row.cost_breakdown_by_model}
    assert set(by_model) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert by_model["claude-sonnet-4-6"].calls == 100
    # 10000 * 3/1e6 + 2000 * 15/1e6 = 0.03 + 0.03 = 0.06
    assert by_model["claude-sonnet-4-6"].cost_usd == pytest.approx(0.06)
    assert by_model["claude-haiku-4-5"].calls == 50
    # 8000 * 1/1e6 + 1000 * 5/1e6 = 0.008 + 0.005 = 0.013
    assert by_model["claude-haiku-4-5"].cost_usd == pytest.approx(0.013)
    assert row.cost_usd == pytest.approx(0.073)
    assert row.cost_usd_byok == 0.0


@pytest.mark.asyncio
async def test_byok_never_contributes_to_cost_usd(db_session):
    """A ``paid_by='byok'`` row's cost lands in cost_usd_byok, not cost_usd."""
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-byok"

    platform_report = _make_report(
        user_id=user_id, workspace_id=workspace_id, started_at=datetime(2026, 4, 5, 3, 0)
    )
    byok_report = _make_report(
        user_id=user_id,
        workspace_id=workspace_id,
        started_at=datetime(2026, 4, 5, 4, 0),
        source="analysis",
        paid_by="byok",
    )
    db_session.add_all([platform_report, byok_report])
    await db_session.flush()

    db_session.add_all(
        [
            _make_usage(
                report_id=platform_report.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=10,
                input_tokens=1_000_000,  # $3
                output_tokens=0,
            ),
            _make_usage(
                report_id=byok_report.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=5,
                input_tokens=2_000_000,  # $6
                output_tokens=0,
            ),
        ]
    )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.cost_usd == pytest.approx(3.0)
    assert row.cost_usd_byok == pytest.approx(6.0)

    # Cross-check: the by_source totals must reconcile with the row totals.
    by_source = {b.source: b for b in row.cost_breakdown_by_source}
    assert by_source["sleep"].cost_usd == pytest.approx(3.0)
    assert by_source["sleep"].cost_usd_byok == 0.0
    assert by_source["analysis"].cost_usd == 0.0
    assert by_source["analysis"].cost_usd_byok == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_pricing_miss_surfaces_cost_unknown_as_null(db_session):
    """A usage row with no resolved pricing yields cost_usd = None.

    "cost unknown" must be distinguishable from "genuinely $0" so the
    dashboard renders "—" instead of "$0.00" for legacy / unpriced
    models. Same semantics as ``LLMPricingService.lookup()`` returning
    ``None`` on a miss. Usage figures (calls / tokens_in / tokens_out)
    are still surfaced — only the cost is NULL.
    """
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-unpriced"

    report = _make_report(
        user_id=user_id, workspace_id=workspace_id, started_at=datetime(2026, 4, 5, 3, 0)
    )
    db_session.add(report)
    await db_session.flush()

    db_session.add(
        _make_usage(
            report_id=report.id,
            provider="exotic-vendor",
            model="not-yet-priced-1.0",
            calls=10,
            input_tokens=1_000_000,
            output_tokens=500_000,
        )
    )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    # Usage preserved
    assert row.calls == 10
    assert row.tokens_in == 1_000_000
    assert row.tokens_out == 500_000
    # Cost is None ("cost unknown") because the model has no pricing row.
    assert row.cost_usd is None, "unpriced usage must surface as cost_usd=None, not 0.0"
    assert row.cost_usd_byok == 0.0  # no BYOK contribution at all → stays 0.0
    # Breakdown also propagates None.
    by_model = {b.model: b for b in row.cost_breakdown_by_model}
    assert by_model["not-yet-priced-1.0"].cost_usd is None
    by_source = {b.source: b for b in row.cost_breakdown_by_source}
    assert by_source["sleep"].cost_usd is None


@pytest.mark.asyncio
async def test_pricing_miss_for_one_model_taints_full_aggregate(db_session):
    """Mixed priced+unpriced models in one bucket → cost_usd is NULL overall.

    NULL is sticky: once any contributing row is unpriced, the bucket's
    cost_usd is None for the whole row, the matching by_source entry,
    AND the unpriced model's by_model entry. The PRICED model's by_model
    entry retains its real cost so the operator can see at least the
    portion that IS computable.
    """
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-mixed"

    report = _make_report(
        user_id=user_id, workspace_id=workspace_id, started_at=datetime(2026, 4, 5, 3, 0)
    )
    db_session.add(report)
    await db_session.flush()

    # Two usage rows under one report — one priced model, one unpriced.
    db_session.add_all(
        [
            _make_usage(
                report_id=report.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=10,
                input_tokens=1_000_000,  # $3
                output_tokens=0,
            ),
            _make_usage(
                report_id=report.id,
                provider="exotic-vendor",
                model="not-yet-priced-1.0",
                calls=5,
                input_tokens=500_000,
                output_tokens=0,
            ),
        ]
    )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 15
    # Total cost is None because the by_source aggregate sums priced+unpriced.
    assert row.cost_usd is None
    # Per-model breakdown: priced model keeps its $3, unpriced is None.
    by_model = {b.model: b for b in row.cost_breakdown_by_model}
    assert by_model["claude-sonnet-4-6"].cost_usd == pytest.approx(3.0)
    assert by_model["not-yet-priced-1.0"].cost_usd is None
    # Per-source breakdown collapses both → None (sticky NULL).
    by_source = {b.source: b for b in row.cost_breakdown_by_source}
    assert by_source["sleep"].cost_usd is None


@pytest.mark.asyncio
async def test_embedding_cost_attributed_via_sleep_reports_columns(db_session):
    """``embedding_tokens`` × embedding price contributes to cost_usd."""
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-embedding"

    report = _make_report(
        user_id=user_id,
        workspace_id=workspace_id,
        started_at=datetime(2026, 4, 5, 3, 0),
        embedding_tokens=10_000_000,  # 10M × $0.02/1M = $0.20
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    db_session.add(report)
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.embedding_tokens == 10_000_000
    assert row.cost_usd == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_filter_combination_source_and_paid_by(db_session):
    """``source=analysis & paid_by=byok`` returns only the intersection."""
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-filter"
    common = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "started_at": datetime(2026, 4, 5, 3, 0),
    }
    reports = [
        _make_report(**common, source="sleep", paid_by="platform"),
        _make_report(**common, source="analysis", paid_by="platform"),
        _make_report(**common, source="sleep", paid_by="byok"),
        _make_report(**common, source="analysis", paid_by="byok"),  # the keep
    ]
    db_session.add_all(reports)
    await db_session.flush()

    for r in reports:
        db_session.add(
            _make_usage(
                report_id=r.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=1,
                input_tokens=1_000_000,
                output_tokens=0,
            )
        )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
        source="analysis",
        paid_by="byok",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 1  # only the analysis × byok row, not all 4
    assert row.cost_usd == 0.0  # filtered to BYOK so platform total = 0
    assert row.cost_usd_byok == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_invalid_period_raises_value_error(db_session):
    """``period`` outside the allowlist raises ValueError before SQL fires."""
    service = CostAggregationService(db_session)
    with pytest.raises(ValueError, match="Invalid period"):
        await service.aggregate(
            period="hour",
            start=datetime(2026, 4, 1),
            end=datetime(2026, 4, 8),
        )


@pytest.mark.asyncio
async def test_inverted_window_raises_value_error(db_session):
    """``start >= end`` raises ValueError immediately."""
    service = CostAggregationService(db_session)
    with pytest.raises(ValueError, match="start must be < end"):
        await service.aggregate(
            period="day",
            start=datetime(2026, 4, 8),
            end=datetime(2026, 4, 1),
        )


@pytest.mark.asyncio
async def test_period_week_collapses_seven_daily_reports_into_one_bucket(db_session):
    """Seven reports across one ISO week roll into a single weekly bucket.

    Postgres ``date_trunc('week', ...)`` snaps to the ISO Monday. Seeding
    Mon–Sun in the same ISO week verifies that the bucket key is computed
    correctly and that all seven daily reports collapse to one row, not
    seven. Without this assertion the route layer's ``period=week``
    contract is only exercised in SQL but never verified at the row count.
    """
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-weekly"

    # 2026-04-06 is a Monday → 2026-04-12 is the following Sunday. All
    # seven dates share the same ISO week, so date_trunc('week', ...)
    # returns 2026-04-06 for every row.
    week_dates = [datetime(2026, 4, d, 12, 0) for d in range(6, 13)]
    reports = [
        _make_report(user_id=user_id, workspace_id=workspace_id, started_at=ts) for ts in week_dates
    ]
    db_session.add_all(reports)
    await db_session.flush()

    for r in reports:
        db_session.add(
            _make_usage(
                report_id=r.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=1,
                input_tokens=1_000_000,  # $3 each → $21 total for the week
                output_tokens=0,
            )
        )
    await db_session.flush()

    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="week",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 15),
        workspace_id=workspace_id,
    )

    assert len(rows) == 1, "seven daily reports must collapse into one weekly bucket"
    row = rows[0]
    assert row.period_start == date(2026, 4, 6)  # ISO Monday
    assert row.calls == 7
    assert row.tokens_in == 7_000_000
    assert row.cost_usd == pytest.approx(21.0)


@pytest.mark.asyncio
async def test_window_upper_bound_includes_late_day_records(db_session):
    """A report at 23:59:59 on the route's ``to`` day is still included.

    The route layer turns ``to=YYYY-MM-DD`` into a half-open
    ``[start, end)`` window where ``end = (to + 1 day) at 00:00:00``. A
    record at the very end of the ``to`` day (23:59:59) MUST be included;
    a record one second later (the next day 00:00:00) MUST be excluded.
    Without this assertion, an off-by-one in the route's window
    construction would silently drop the last second of every query.
    """
    await _seed_pricing(db_session)

    workspace_id = uuid4()
    user_id = "user-boundary"

    # Two reports: one at the very end of the to-day (must include),
    # one at the start of the day after (must exclude). Same workspace
    # + user so the inclusion/exclusion shows up as a count delta.
    in_window = _make_report(
        user_id=user_id,
        workspace_id=workspace_id,
        started_at=datetime(2026, 4, 7, 23, 59, 59),
    )
    out_of_window = _make_report(
        user_id=user_id,
        workspace_id=workspace_id,
        started_at=datetime(2026, 4, 8, 0, 0, 0),
    )
    db_session.add_all([in_window, out_of_window])
    await db_session.flush()

    for r in (in_window, out_of_window):
        db_session.add(
            _make_usage(
                report_id=r.id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                calls=1,
                input_tokens=1_000_000,  # $3
                output_tokens=0,
            )
        )
    await db_session.flush()

    service = CostAggregationService(db_session)
    # Mirror the route's window math: to=2026-04-07 → end=2026-04-08T00:00.
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )

    assert len(rows) == 1, "only the in-window report should appear"
    row = rows[0]
    assert row.period_start == date(2026, 4, 7)
    assert row.calls == 1
    assert row.cost_usd == pytest.approx(3.0)
