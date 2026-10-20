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
async def test_window_exceeding_cap_raises_value_error(db_session):
    """A window wider than ``MAX_LOOKBACK_DAYS`` raises ValueError before SQL.

    Defense-in-depth guardrail: a non-UI caller (curl / SDK / MCP) that
    bypasses the frontend's 365-day soft cap must not be able to scan
    years of ``sleep_reports``. 366 days is one past the boundary.
    """
    service = CostAggregationService(db_session)
    with pytest.raises(ValueError, match="exceeds"):
        await service.aggregate(
            period="day",
            start=datetime(2026, 1, 1),
            end=datetime(2027, 1, 2),  # 366-day half-open window
        )


@pytest.mark.asyncio
async def test_window_at_cap_boundary_is_allowed(db_session):
    """Exactly ``MAX_LOOKBACK_DAYS`` (365) does NOT raise — pins off-by-one.

    The half-open window ``end - start`` equals the frontend's inclusive
    day count (``end = to + 1 day``), so a 365-day backend window is the
    UI's maximum permitted selection and must pass. Empty DB → empty list.
    """
    service = CostAggregationService(db_session)
    rows = await service.aggregate(
        period="day",
        start=datetime(2026, 1, 1),
        end=datetime(2027, 1, 1),  # 365-day half-open window (2026 is not a leap year)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_sub_day_overage_message_reports_precise_span(db_session):
    """A sub-day overage past the cap is rejected with a precise message.

    The cap uses ``> timedelta`` so 365 days + 1 hour is rejected, but
    reporting ``.days`` would truncate to a misleading "365 days" — the
    *allowed* boundary. The message must carry the sub-day remainder so it
    matches the predicate that fired. (Copilot review, PR #863.)
    """
    service = CostAggregationService(db_session)
    with pytest.raises(ValueError, match="exceeds") as exc:
        await service.aggregate(
            period="day",
            start=datetime(2026, 1, 1, 0, 0),
            end=datetime(2027, 1, 1, 1, 0),  # 365 days + 1 hour
        )
    # Must not claim a bare "365 days" (the allowed boundary) — the message
    # carries the sub-day remainder that pushed the window past the cap.
    assert "1:00:00" in str(exc.value)


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


# ----------------------------------------------------------------------
# #2019: the llm_call_log leg (event-shaped callers outside Sleep)
# ----------------------------------------------------------------------


def _make_ledger_row(
    *,
    user_id: str,
    workspace_id: UUID,
    occurred_at: datetime,
    caller: str = "supersede_verify",
    call_type: str = "completion",
    model: str = "claude-haiku-4-5",
    input_tokens: int | None = 1_000_000,
    output_tokens: int | None = 100_000,
    embedding_tokens: int | None = None,
    cost_usd: str = "1.5",
    paid_by: str = "platform",
    pricing_miss: bool = False,
    call_metadata: dict | None = None,
):
    from models.llm_call_log import LLMCallLog

    if call_metadata is None:
        call_metadata = {"pricing_miss": True} if pricing_miss else {"verdict": "parallel"}

    return LLMCallLog(
        occurred_at=occurred_at,
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=uuid4(),
        caller=caller,
        call_type=call_type,
        provider="anthropic" if call_type == "completion" else "openai",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        embedding_tokens=embedding_tokens,
        cost_usd=Decimal(cost_usd),
        paid_by=paid_by,
        call_metadata=call_metadata,
    )


@pytest.mark.asyncio
async def test_supersede_verify_ledger_rows_surface_as_their_own_source(db_session):
    """Judge calls (#2019) add calls, tokens and their write-time cost, and show
    up as ``source='supersede_verify'`` beside the Sleep source."""
    await _seed_pricing(db_session)
    workspace_id = uuid4()
    user_id = "user-ledger"
    at = datetime(2026, 4, 5, 3, 0)

    report = _make_report(user_id=user_id, workspace_id=workspace_id, started_at=at)
    db_session.add(report)
    await db_session.flush()
    db_session.add(
        _make_usage(
            report_id=report.id,
            provider="anthropic",
            model="claude-haiku-4-5",
            calls=2,
            input_tokens=1_000_000,  # $1
            output_tokens=0,
        )
    )
    db_session.add_all(
        [
            _make_ledger_row(user_id=user_id, workspace_id=workspace_id, occurred_at=at),
            _make_ledger_row(
                user_id=user_id, workspace_id=workspace_id, occurred_at=at, cost_usd="0.5"
            ),
        ]
    )
    await db_session.flush()

    rows = await CostAggregationService(db_session).aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 4
    assert row.tokens_in == 3_000_000
    assert row.tokens_out == 200_000
    assert row.cost_usd == pytest.approx(3.0)  # $1 Sleep + $1.5 + $0.5 ledger
    by_source = {b.source: b for b in row.cost_breakdown_by_source}
    assert set(by_source) == {"sleep", "supersede_verify"}
    assert by_source["supersede_verify"].calls == 2
    assert by_source["supersede_verify"].cost_usd == pytest.approx(2.0)
    by_model = {b.model: b for b in row.cost_breakdown_by_model}
    assert by_model["claude-haiku-4-5"].calls == 4


@pytest.mark.asyncio
async def test_ledger_pricing_miss_is_cost_unknown(db_session):
    workspace_id = uuid4()
    db_session.add(
        _make_ledger_row(
            user_id="user-miss",
            workspace_id=workspace_id,
            occurred_at=datetime(2026, 4, 5),
            cost_usd="0",
            pricing_miss=True,
        )
    )
    await db_session.flush()
    rows = await CostAggregationService(db_session).aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].calls == 1 and rows[0].cost_usd is None


@pytest.mark.asyncio
async def test_ledger_leg_honours_filters_and_skips_other_callers(db_session):
    """Recall embeddings stay out (not part of this view yet); source /
    paid_by / user filters apply to the ledger leg as to Sleep."""
    await _seed_pricing(db_session)
    workspace_id = uuid4()
    at = datetime(2026, 4, 5, 3, 0)
    report = _make_report(user_id="user-a", workspace_id=workspace_id, started_at=at)
    db_session.add(report)
    await db_session.flush()
    db_session.add(
        _make_usage(
            report_id=report.id,
            provider="anthropic",
            model="claude-haiku-4-5",
            calls=1,
            input_tokens=1_000_000,
            output_tokens=0,
        )
    )
    db_session.add_all(
        [
            _make_ledger_row(user_id="user-a", workspace_id=workspace_id, occurred_at=at),
            _make_ledger_row(
                user_id="user-a",
                workspace_id=workspace_id,
                occurred_at=at,
                paid_by="byok",
                cost_usd="0.25",
            ),
            _make_ledger_row(user_id="user-b", workspace_id=workspace_id, occurred_at=at),
            _make_ledger_row(
                user_id="user-a",
                workspace_id=workspace_id,
                occurred_at=at,
                caller="recall",
                call_type="embedding",
                model="text-embedding-3-small",
                input_tokens=None,
                output_tokens=None,
                embedding_tokens=1_000,
                cost_usd="0.00002",
            ),
        ]
    )
    await db_session.flush()
    service = CostAggregationService(db_session)
    window = {"period": "day", "start": datetime(2026, 4, 1), "end": datetime(2026, 4, 8)}

    rows = await service.aggregate(**window, workspace_id=workspace_id, user_id="user-a")
    assert len(rows) == 1
    assert rows[0].calls == 3  # Sleep 1 + two judge calls; the recall row stays out
    assert rows[0].embedding_tokens == 0
    assert rows[0].cost_usd == pytest.approx(2.5)
    assert rows[0].cost_usd_byok == pytest.approx(0.25)

    only_judge = await service.aggregate(
        **window, workspace_id=workspace_id, source="supersede_verify", paid_by="platform"
    )
    assert sorted((r.user_id, r.calls) for r in only_judge) == [("user-a", 1), ("user-b", 1)]
    assert all(
        [b.source for b in r.cost_breakdown_by_source] == ["supersede_verify"] for r in only_judge
    )

    only_sleep = await service.aggregate(**window, workspace_id=workspace_id, source="sleep")
    assert [(r.user_id, r.calls) for r in only_sleep] == [("user-a", 1)]


# ----------------------------------------------------------------------
# #2154: the recall LLM selector stage (#2144) as its own ledger source
# ----------------------------------------------------------------------


def _selector_row(**kwargs):
    """A selector completion row: ``caller='recall'`` + the stage marker."""
    outcome = kwargs.pop("outcome", "applied")
    return _make_ledger_row(
        caller="recall",
        call_metadata={"stage": "llm_selector", "outcome": outcome, "rows_sent": 10},
        **kwargs,
    )


def _recall_embedding_row(**kwargs):
    return _make_ledger_row(
        caller="recall",
        call_type="embedding",
        model="text-embedding-3-small",
        input_tokens=None,
        output_tokens=None,
        embedding_tokens=1_000,
        cost_usd="0.00002",
        call_metadata={},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_selector_rows_surface_as_llm_selector_beside_judge_and_sleep(db_session):
    """Selector completions show as ``source='llm_selector'`` with their
    write-time cost; recall embeddings (same caller) and a recall completion
    without the stage stay out."""
    await _seed_pricing(db_session)
    workspace_id = uuid4()
    user_id = "user-selector"
    at = datetime(2026, 4, 5, 3, 0)
    ids = {"user_id": user_id, "workspace_id": workspace_id, "occurred_at": at}

    report = _make_report(user_id=user_id, workspace_id=workspace_id, started_at=at)
    db_session.add(report)
    await db_session.flush()
    db_session.add(
        _make_usage(
            report_id=report.id,
            provider="anthropic",
            model="claude-haiku-4-5",
            calls=1,
            input_tokens=1_000_000,  # $1
            output_tokens=0,
        )
    )
    db_session.add_all(
        [
            _make_ledger_row(**ids, cost_usd="0.5"),  # judge
            _selector_row(**ids, cost_usd="0.25", input_tokens=2_000, output_tokens=50),
            _selector_row(
                **ids,
                cost_usd="0.75",
                input_tokens=3_000,
                output_tokens=60,
                outcome="invalid_answer",
            ),
            _recall_embedding_row(**ids),
            _make_ledger_row(**ids, caller="recall", cost_usd="9", call_metadata={}),
        ]
    )
    await db_session.flush()

    rows = await CostAggregationService(db_session).aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 4  # Sleep 1 + judge 1 + selector 2
    assert row.embedding_tokens == 0
    assert row.tokens_in == 1_000_000 + 1_000_000 + 5_000
    assert row.cost_usd == pytest.approx(2.5)  # $1 Sleep + $0.5 judge + $1 selector
    by_source = {b.source: b for b in row.cost_breakdown_by_source}
    assert set(by_source) == {"sleep", "supersede_verify", "llm_selector"}
    assert by_source["llm_selector"].calls == 2
    assert by_source["llm_selector"].cost_usd == pytest.approx(1.0)
    assert by_source["supersede_verify"].cost_usd == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_source_filter_llm_selector_returns_selector_rows_only(db_session):
    workspace_id = uuid4()
    at = datetime(2026, 4, 5, 3, 0)
    db_session.add_all(
        [
            _make_ledger_row(user_id="user-a", workspace_id=workspace_id, occurred_at=at),
            _selector_row(
                user_id="user-a", workspace_id=workspace_id, occurred_at=at, cost_usd="0.2"
            ),
            _selector_row(
                user_id="user-b",
                workspace_id=workspace_id,
                occurred_at=at,
                cost_usd="0.3",
                paid_by="byok",
            ),
            _recall_embedding_row(user_id="user-a", workspace_id=workspace_id, occurred_at=at),
        ]
    )
    await db_session.flush()
    service = CostAggregationService(db_session)
    window = {"period": "day", "start": datetime(2026, 4, 1), "end": datetime(2026, 4, 8)}

    only_selector = await service.aggregate(
        **window, workspace_id=workspace_id, source="llm_selector"
    )
    assert sorted((r.user_id, r.calls) for r in only_selector) == [("user-a", 1), ("user-b", 1)]
    assert all(
        [b.source for b in r.cost_breakdown_by_source] == ["llm_selector"] for r in only_selector
    )
    by_user = {r.user_id: r for r in only_selector}
    assert by_user["user-a"].cost_usd == pytest.approx(0.2)
    assert by_user["user-b"].cost_usd_byok == pytest.approx(0.3)

    platform_selector = await service.aggregate(
        **window, workspace_id=workspace_id, source="llm_selector", paid_by="platform"
    )
    assert [(r.user_id, r.calls) for r in platform_selector] == [("user-a", 1)]

    only_judge = await service.aggregate(
        **window, workspace_id=workspace_id, source="supersede_verify"
    )
    assert [(r.user_id, r.calls) for r in only_judge] == [("user-a", 1)]


@pytest.mark.asyncio
async def test_timed_out_selector_call_counts_as_a_call_with_unknown_cost(db_session):
    """A timed-out call is written with zero tokens and ``cost_usd = 0``: the
    request may have been billed, so the bucket is "cost unknown", not $0."""
    workspace_id = uuid4()
    at = datetime(2026, 4, 5, 3, 0)
    ids = {"user_id": "user-timeout", "workspace_id": workspace_id, "occurred_at": at}
    db_session.add_all(
        [
            _selector_row(**ids, cost_usd="0.2"),
            _selector_row(
                **ids,
                cost_usd="0",
                input_tokens=0,
                output_tokens=0,
                outcome="timeout",
            ),
        ]
    )
    await db_session.flush()

    rows = await CostAggregationService(db_session).aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 2
    assert row.cost_usd is None
    (selector,) = row.cost_breakdown_by_source
    assert selector.source == "llm_selector"
    assert selector.calls == 2
    assert selector.cost_usd is None


@pytest.mark.asyncio
async def test_zero_token_judge_row_without_timeout_stays_priced(db_session):
    """Only a timed-out call is "cost unknown": a judge row from a provider that
    reports no usage keeps its stored $0, as before #2154."""
    workspace_id = uuid4()
    db_session.add(
        _make_ledger_row(
            user_id="user-no-usage",
            workspace_id=workspace_id,
            occurred_at=datetime(2026, 4, 5),
            input_tokens=0,
            output_tokens=0,
            cost_usd="0",
        )
    )
    await db_session.flush()
    rows = await CostAggregationService(db_session).aggregate(
        period="day",
        start=datetime(2026, 4, 1),
        end=datetime(2026, 4, 8),
        workspace_id=workspace_id,
    )
    assert len(rows) == 1
    assert rows[0].calls == 1 and rows[0].cost_usd == pytest.approx(0.0)
