"""Coverage tests for ``services/analysis/reporter`` (Stage [J] persist).

Two layers:

1. **Pure helpers** (no DB): ``_aggregate_breakdown_totals``,
   ``_compute_actual_cost_cents``, ``_build_cluster_row``,
   ``_index_members_by_cluster``. These exercise the cost-accounting
   math (rounding, cache-read fallback, zero/empty), the per-cluster
   ORM-row construction (centroid mean vs empty fallback, malformed
   representative ids), and the O(n) member-index pass (including the
   ``setdefault`` branch for an out-of-range label).

2. **DB-backed persist** (``db_session``): ``persist_results`` happy
   path, the concurrent-cancel guard (status flipped to ``cancelled``
   mid-compute), and ``persist_failure`` happy / cancel / truncation
   branches. The orchestrator owns the transaction; the tests run
   inside the session fixture and flush to make the writes observable.

No LLM or network call is ever made — the labeler output is fed in as
``ClusterLabel`` dataclasses with pre-computed ``LLMCallBreakdown``s.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select

from models.analysis import (
    MemoryAnalysis,
    MemoryAnalysisAssignment,
    MemoryAnalysisCluster,
)
from models.sleep import SleepReport, SleepReportLLMUsage
from services.analysis.labeler import ClusterLabel
from services.analysis.reporter import (
    PersistInputs,
    _aggregate_breakdown_totals,
    _build_cluster_row,
    _compute_actual_cost_cents,
    _CostTotals,
    _index_members_by_cluster,
    persist_failure,
    persist_results,
)
from services.analysis.vector_pull import MemoryRecord
from services.sleep.reporter import LLMCallBreakdown
from utils.datetime import utcnow


# --------------------------------------------------------------------------
# Builders for the pure-helper inputs.
# --------------------------------------------------------------------------
def _breakdown(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    calls: int = 1,
    provider: str = "openai",
    model: str = "gpt-5-nano",
) -> LLMCallBreakdown:
    return LLMCallBreakdown(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        calls=calls,
    )


def _cluster_label(
    *,
    cluster_index: int = 0,
    label: str = "alpha",
    description: str = "desc",
    label_confidence: float = 0.9,
    representative_memory_ids: list[str] | None = None,
    breakdown: LLMCallBreakdown | None = None,
    failed: bool = False,
) -> ClusterLabel:
    return ClusterLabel(
        cluster_index=cluster_index,
        label=label,
        description=description,
        label_confidence=label_confidence,
        representative_memory_ids=representative_memory_ids
        if representative_memory_ids is not None
        else [],
        breakdown=breakdown,
        failed=failed,
    )


def _memory_record(
    *,
    mem_id: UUID | None = None,
    type_: str = "note",
    tags: list[str] | None = None,
    importance: float = 0.5,
    created_at: datetime | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=mem_id or uuid4(),
        type=type_,
        summary="summary text",
        tags=tags if tags is not None else ["t1"],
        importance=importance,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
    )


class TestAggregateBreakdownTotals:
    """``_aggregate_breakdown_totals``: single-pass extraction + summing."""

    def test_empty_list_yields_zero_totals(self):
        """No cluster labels → no breakdowns and all-zero totals."""
        breakdowns, totals = _aggregate_breakdown_totals([])
        assert breakdowns == []
        assert totals == _CostTotals(
            input_tokens=0, output_tokens=0, cached_input_tokens=0, calls=0
        )

    def test_none_breakdowns_are_skipped(self):
        """Clusters whose ``breakdown`` is None contribute nothing."""
        labels = [
            _cluster_label(cluster_index=0, breakdown=None),
            _cluster_label(cluster_index=1, breakdown=None),
        ]
        breakdowns, totals = _aggregate_breakdown_totals(labels)
        assert breakdowns == []
        assert totals.calls == 0
        assert totals.input_tokens == 0

    def test_sums_across_multiple_breakdowns(self):
        """Totals are the field-wise sum; only non-None breakdowns collected."""
        labels = [
            _cluster_label(
                cluster_index=0,
                breakdown=_breakdown(
                    input_tokens=100, output_tokens=20, cached_input_tokens=5, calls=1
                ),
            ),
            _cluster_label(cluster_index=1, breakdown=None),  # skipped
            _cluster_label(
                cluster_index=2,
                breakdown=_breakdown(
                    input_tokens=300, output_tokens=40, cached_input_tokens=15, calls=2
                ),
            ),
        ]
        breakdowns, totals = _aggregate_breakdown_totals(labels)
        assert len(breakdowns) == 2
        assert totals.input_tokens == 400
        assert totals.output_tokens == 60
        assert totals.cached_input_tokens == 20
        assert totals.calls == 3


class TestComputeActualCostCents:
    """``_compute_actual_cost_cents``: snapshot-priced rounded-up cents."""

    def test_zero_tokens_returns_zero(self):
        """No tokens → cost_usd is 0, so returns 0 (not the min-1 floor)."""
        totals = _CostTotals(input_tokens=0, output_tokens=0, cached_input_tokens=0, calls=0)
        snapshot = {"rates": {"input_tokens": 1.0, "output_tokens": 2.0}}
        assert _compute_actual_cost_cents(totals, snapshot) == 0

    def test_missing_rates_key_treats_prices_as_zero(self):
        """A snapshot with no ``rates`` prices everything at 0 → 0 cents."""
        totals = _CostTotals(
            input_tokens=1_000_000, output_tokens=1_000_000, cached_input_tokens=0, calls=1
        )
        assert _compute_actual_cost_cents(totals, {}) == 0

    def test_rounds_up_to_at_least_one_cent(self):
        """A tiny positive cost rounds up to the 1-cent floor via ceil/max."""
        # 1000 input tokens at $0.20/M = $0.0002 → 0.02 cents → ceil → 1 cent.
        totals = _CostTotals(input_tokens=1_000, output_tokens=0, cached_input_tokens=0, calls=1)
        snapshot = {"rates": {"input_tokens": 0.20, "output_tokens": 0.0}}
        assert _compute_actual_cost_cents(totals, snapshot) == 1

    def test_full_pricing_with_explicit_cache_rate(self):
        """All three token classes priced from the snapshot, ceil to cents."""
        totals = _CostTotals(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            calls=3,
        )
        snapshot = {
            "rates": {
                "input_tokens": 1.0,  # $1.00 / M
                "output_tokens": 2.0,  # $2.00 / M
                "cache_read_tokens": 0.5,  # $0.50 / M
            }
        }
        # cost_usd = 1.0 + 2.0 + 0.5 = $3.50 → 350 cents.
        assert _compute_actual_cost_cents(totals, snapshot) == 350

    def test_cache_rate_defaults_to_ten_percent_of_input(self):
        """When ``cache_read_tokens`` is absent, cached input is 10% of input rate."""
        totals = _CostTotals(
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=10_000_000,
            calls=1,
        )
        snapshot = {"rates": {"input_tokens": 1.0, "output_tokens": 0.0}}
        # cached_per_m = 1.0 / 10 = 0.1 → 10M * 0.1 / 1M = $1.00 → 100 cents.
        assert _compute_actual_cost_cents(totals, snapshot) == 100


class TestIndexMembersByCluster:
    """``_index_members_by_cluster``: O(n) per-cluster member positions."""

    def test_basic_grouping(self):
        """Positions are grouped by their label value, as int64 arrays."""
        labels = np.array([0, 1, 0, 1, 0])
        result = _index_members_by_cluster(labels, n_clusters=2)
        assert set(result.keys()) == {0, 1}
        np.testing.assert_array_equal(result[0], np.array([0, 2, 4]))
        np.testing.assert_array_equal(result[1], np.array([1, 3]))
        assert result[0].dtype == np.int64

    def test_empty_cluster_index_present_with_empty_array(self):
        """A pre-seeded cluster index with no members yields an empty array."""
        labels = np.array([0, 0, 0])
        result = _index_members_by_cluster(labels, n_clusters=3)
        # indices 1 and 2 were pre-seeded but never assigned.
        assert result[1].size == 0
        assert result[2].size == 0
        np.testing.assert_array_equal(result[0], np.array([0, 1, 2]))

    def test_out_of_range_label_uses_setdefault_branch(self):
        """A label >= n_clusters is added via setdefault (defensive branch)."""
        labels = np.array([0, 5])  # 5 was not pre-seeded by range(n_clusters)
        result = _index_members_by_cluster(labels, n_clusters=1)
        assert 5 in result
        np.testing.assert_array_equal(result[5], np.array([1]))

    def test_no_members_all_empty(self):
        """Empty label array → every pre-seeded index maps to an empty array."""
        labels = np.array([], dtype=np.int64)
        result = _index_members_by_cluster(labels, n_clusters=2)
        assert result[0].size == 0
        assert result[1].size == 0


class TestBuildClusterRow:
    """``_build_cluster_row``: one ORM row from a ClusterLabel + members."""

    def test_centroid_is_member_mean_when_nonempty(self):
        """Centroid_2d is the mean of member coords; count == member count."""
        coords = np.array([[0.0, 0.0], [2.0, 4.0], [4.0, 8.0]])
        member_pos = np.array([0, 1, 2])
        members = [_memory_record() for _ in range(3)]
        cluster_id = uuid4()
        analysis_id = uuid4()
        row = _build_cluster_row(
            cluster_id=cluster_id,
            analysis_id=analysis_id,
            cluster_label=_cluster_label(cluster_index=7, label="L", description="D"),
            member_pos=member_pos,
            coords_2d=coords,
            member_memories=members,
            window_from=None,
            window_to=None,
        )
        assert isinstance(row, MemoryAnalysisCluster)
        assert row.id == cluster_id
        assert row.analysis_id == analysis_id
        assert row.parent_id is None
        assert row.cluster_index == 7
        assert row.label == "L"
        assert row.description == "D"
        assert row.count == 3
        # mean of x = (0+2+4)/3 = 2.0 ; mean of y = (0+4+8)/3 = 4.0
        assert row.centroid_2d == [2.0, 4.0]
        assert row.label_confidence == pytest.approx(0.9)
        assert isinstance(row.property_stats, dict)

    def test_empty_members_fallback_centroid_zero(self):
        """No members → centroid falls back to [0.0, 0.0] and count 0."""
        coords = np.array([[1.0, 1.0]])
        member_pos = np.empty(0, dtype=np.int64)
        row = _build_cluster_row(
            cluster_id=uuid4(),
            analysis_id=uuid4(),
            cluster_label=_cluster_label(cluster_index=0),
            member_pos=member_pos,
            coords_2d=coords,
            member_memories=[],
            window_from=None,
            window_to=None,
        )
        assert row.centroid_2d == [0.0, 0.0]
        assert row.count == 0

    def test_empty_description_normalized_to_none(self):
        """An empty-string description becomes None (``description or None``)."""
        row = _build_cluster_row(
            cluster_id=uuid4(),
            analysis_id=uuid4(),
            cluster_label=_cluster_label(cluster_index=0, description=""),
            member_pos=np.empty(0, dtype=np.int64),
            coords_2d=np.empty((0, 2)),
            member_memories=[],
            window_from=None,
            window_to=None,
        )
        assert row.description is None

    def test_valid_representative_ids_parsed_to_uuid(self):
        """Well-formed UUID strings are parsed into UUID objects."""
        rid1 = str(uuid4())
        rid2 = str(uuid4())
        row = _build_cluster_row(
            cluster_id=uuid4(),
            analysis_id=uuid4(),
            cluster_label=_cluster_label(cluster_index=0, representative_memory_ids=[rid1, rid2]),
            member_pos=np.empty(0, dtype=np.int64),
            coords_2d=np.empty((0, 2)),
            member_memories=[],
            window_from=None,
            window_to=None,
        )
        assert row.representative_memory_ids == [UUID(rid1), UUID(rid2)]

    def test_malformed_representative_ids_are_skipped(self):
        """Non-UUID strings (and None) are silently dropped, valid ones kept."""
        good = str(uuid4())
        row = _build_cluster_row(
            cluster_id=uuid4(),
            analysis_id=uuid4(),
            cluster_label=_cluster_label(
                cluster_index=0,
                representative_memory_ids=["not-a-uuid", good, ""],
            ),
            member_pos=np.empty(0, dtype=np.int64),
            coords_2d=np.empty((0, 2)),
            member_memories=[],
            window_from=None,
            window_to=None,
        )
        assert row.representative_memory_ids == [UUID(good)]

    def test_property_stats_reflect_member_facets(self):
        """property_stats is computed from member facets (types distribution)."""
        coords = np.array([[0.0, 0.0], [1.0, 1.0]])
        member_pos = np.array([0, 1])
        members = [
            _memory_record(type_="note", tags=["a"], importance=0.1),
            _memory_record(type_="code", tags=["a", "b"], importance=0.9),
        ]
        row = _build_cluster_row(
            cluster_id=uuid4(),
            analysis_id=uuid4(),
            cluster_label=_cluster_label(cluster_index=0),
            member_pos=member_pos,
            coords_2d=coords,
            member_memories=members,
            window_from=None,
            window_to=None,
        )
        # aggregate_cluster_stats emits a "types" key with the distribution.
        assert "types" in row.property_stats
        types = row.property_stats["types"]
        # Both member types should be represented.
        assert "note" in types
        assert "code" in types


# --------------------------------------------------------------------------
# DB-backed persist tests.
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def fixture_workspace_id(db_session) -> UUID:
    from models.auth import Workspace

    ws = Workspace(id=uuid4(), name="RepWS", owner_user_id="rep_owner")
    db_session.add(ws)
    await db_session.flush()
    return ws.id


@pytest_asyncio.fixture
async def fixture_context_id(db_session, fixture_workspace_id) -> UUID:
    from models.auth import Context

    ctx = Context(
        id=uuid4(),
        workspace_id=fixture_workspace_id,
        name="rep_ctx",
        display_name="Reporter Context",
        created_by="rep_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()
    return ctx.id


@pytest_asyncio.fixture
async def fixture_pricing(db_session):
    from models.llm_pricing import LLMPricing

    row = LLMPricing(
        provider="openai",
        model="gpt-5-nano",
        unit_type="input_tokens",
        price_per_unit=0.20,
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(row)
    await db_session.flush()
    yield row


async def _make_analysis(
    db_session,
    *,
    workspace_id: UUID,
    context_id: UUID,
    pricing,
    status: str = "running",
    model_snapshot: dict | None = None,
    cancellation_reason: str | None = None,
) -> MemoryAnalysis:
    run = MemoryAnalysis(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        triggered_by="rep_user",
        status=status,
        started_at=utcnow(),
        finished_at=None,
        model_id=pricing.id,
        model_snapshot=model_snapshot
        if model_snapshot is not None
        else {"model": "gpt-5-nano", "rates": {"input_tokens": 1.0, "output_tokens": 2.0}},
        embedding_model="text-embedding-3-small",
        params={},
        input_count=0,
        cost_estimated_cents=5,
        cost_actual_cents=None,
        paid_by="byok",
        cancellation_reason=cancellation_reason,
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def _make_db_memory(db_session, *, workspace_id: UUID, context_id: UUID):
    from models.memory import Memory

    mem = Memory(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        user_id="rep_user",
        summary="db memory summary",
        content="db memory content",
        type="note",
        importance=0.5,
        tags=["x"],
        client="test",
    )
    db_session.add(mem)
    await db_session.flush()
    return mem


class TestPersistResults:
    """``persist_results``: atomic write of clusters + assignments + cost."""

    async def test_happy_path_writes_clusters_assignments_and_cost(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """Full persist: cluster rows, assignment rows, succeeded status, cost, sleep report."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            model_snapshot={"rates": {"input_tokens": 1.0, "output_tokens": 2.0}},
        )
        # Two memories: one per cluster.
        db_mems = [
            await _make_db_memory(
                db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
            )
            for _ in range(2)
        ]
        memories = [
            MemoryRecord(
                id=db_mems[0].id,
                type="note",
                summary="m0",
                tags=["a"],
                importance=0.3,
                created_at=datetime(2026, 1, 1),
            ),
            MemoryRecord(
                id=db_mems[1].id,
                type="code",
                summary="m1",
                tags=["b"],
                importance=0.8,
                created_at=datetime(2026, 1, 2),
            ),
        ]
        cluster_labels_arr = np.array([0, 1])
        coords_2d = np.array([[0.0, 0.0], [1.0, 1.0]])
        cluster_results = [
            _cluster_label(
                cluster_index=0,
                label="cluster-zero",
                breakdown=_breakdown(input_tokens=1_000_000, output_tokens=0, calls=1),
            ),
            _cluster_label(
                cluster_index=1,
                label="cluster-one",
                breakdown=_breakdown(input_tokens=0, output_tokens=500_000, calls=1),
            ),
        ]
        inputs = PersistInputs(
            analysis=analysis,
            memories=memories,
            embeddings=np.zeros((2, 4)),
            cluster_labels=cluster_labels_arr,
            coords_2d=coords_2d,
            cluster_results=cluster_results,
            silhouette=0.42,
            size_variance=0.1,
            outlier_ratio=0.0,
            window_from=None,
            window_to=None,
        )

        await persist_results(
            db_session,
            inputs=inputs,
            user_id="rep_user",
            workspace_id=str(fixture_workspace_id),
            context_id=str(fixture_context_id),
        )
        await db_session.flush()

        # Analysis finalized.
        assert analysis.status == "succeeded"
        assert analysis.finished_at is not None
        # cost: input 1M @ $1/M = $1.00, output 0.5M @ $2/M = $1.00 → $2.00 → 200 cents.
        assert analysis.cost_actual_cents == 200
        assert analysis.quality["silhouette"] == 0.42
        assert analysis.quality["n_clusters"] == 2
        assert analysis.quality["n_memories"] == 2
        assert analysis.quality["labeling_failures"] == 0
        assert analysis.quality["label_confidence"] == pytest.approx(0.9)

        # Cluster rows.
        clusters = (
            (
                await db_session.execute(
                    select(MemoryAnalysisCluster).where(
                        MemoryAnalysisCluster.analysis_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(clusters) == 2
        assert {c.cluster_index for c in clusters} == {0, 1}

        # Assignment rows.
        assignments = (
            (
                await db_session.execute(
                    select(MemoryAnalysisAssignment).where(
                        MemoryAnalysisAssignment.analysis_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(assignments) == 2

        # Sibling sleep_reports row at source='analysis', completed.
        report = (
            (
                await db_session.execute(
                    select(SleepReport).where(SleepReport.context_id == fixture_context_id)
                )
            )
            .scalars()
            .first()
        )
        assert report is not None
        assert report.source == "analysis"
        assert report.paid_by == "byok"
        assert report.status == "completed"

        # LLM usage child rows with phase='cluster_labeling'.
        usage = (
            (
                await db_session.execute(
                    select(SleepReportLLMUsage).where(SleepReportLLMUsage.report_id == report.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(usage) == 2
        assert all(u.phase == "cluster_labeling" for u in usage)

    async def test_skips_when_concurrently_cancelled(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """If status was flipped to 'cancelled' before persist, it returns early."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        mem = await _make_db_memory(
            db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
        )
        memories = [
            MemoryRecord(
                id=mem.id,
                type="note",
                summary="m",
                tags=[],
                importance=0.5,
                created_at=datetime(2026, 1, 1),
            )
        ]
        inputs = PersistInputs(
            analysis=analysis,
            memories=memories,
            embeddings=np.zeros((1, 4)),
            cluster_labels=np.array([0]),
            coords_2d=np.array([[0.0, 0.0]]),
            cluster_results=[_cluster_label(cluster_index=0, breakdown=None)],
            silhouette=0.0,
            size_variance=0.0,
            outlier_ratio=0.0,
            window_from=None,
            window_to=None,
        )

        # Simulate the concurrent soft-cancel: another session flipped the
        # row to 'cancelled'. We mutate + flush so db.refresh() sees it.
        analysis.status = "cancelled"
        analysis.cancellation_reason = "user"
        await db_session.flush()

        await persist_results(
            db_session,
            inputs=inputs,
            user_id="rep_user",
            workspace_id=str(fixture_workspace_id),
            context_id=str(fixture_context_id),
        )

        # Early-return: status stays cancelled, no cost set, no sleep report.
        await db_session.refresh(analysis)
        assert analysis.status == "cancelled"
        assert analysis.cost_actual_cents is None
        report = (
            (
                await db_session.execute(
                    select(SleepReport).where(SleepReport.context_id == fixture_context_id)
                )
            )
            .scalars()
            .first()
        )
        assert report is None

    async def test_failed_cluster_excluded_from_label_confidence(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """A failed cluster bumps labeling_failures and is excluded from the avg."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            model_snapshot={"rates": {}},
        )
        mems = [
            await _make_db_memory(
                db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
            )
            for _ in range(2)
        ]
        memories = [
            MemoryRecord(
                id=m.id,
                type="note",
                summary="m",
                tags=[],
                importance=0.5,
                created_at=datetime(2026, 1, 1),
            )
            for m in mems
        ]
        cluster_results = [
            _cluster_label(cluster_index=0, label_confidence=0.8, failed=False, breakdown=None),
            _cluster_label(
                cluster_index=1, label="", label_confidence=0.0, failed=True, breakdown=None
            ),
        ]
        inputs = PersistInputs(
            analysis=analysis,
            memories=memories,
            embeddings=np.zeros((2, 4)),
            cluster_labels=np.array([0, 1]),
            coords_2d=np.array([[0.0, 0.0], [1.0, 1.0]]),
            cluster_results=cluster_results,
            silhouette=0.1,
            size_variance=0.0,
            outlier_ratio=0.0,
            window_from=None,
            window_to=None,
        )
        await persist_results(
            db_session,
            inputs=inputs,
            user_id="rep_user",
            workspace_id=str(fixture_workspace_id),
            context_id=str(fixture_context_id),
        )
        assert analysis.quality["labeling_failures"] == 1
        # Only the non-failed cluster's 0.8 counts.
        assert analysis.quality["label_confidence"] == pytest.approx(0.8)
        # No breakdowns → zero cost.
        assert analysis.cost_actual_cents == 0

    async def test_memory_with_unknown_cluster_index_is_unassigned(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """A label pointing at a non-existent cluster id is skipped (no assignment)."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            model_snapshot={"rates": {}},
        )
        mems = [
            await _make_db_memory(
                db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
            )
            for _ in range(2)
        ]
        memories = [
            MemoryRecord(
                id=m.id,
                type="note",
                summary="m",
                tags=[],
                importance=0.5,
                created_at=datetime(2026, 1, 1),
            )
            for m in mems
        ]
        # Only one cluster (index 0) exists, but memory 1 is labeled 9.
        cluster_results = [_cluster_label(cluster_index=0, breakdown=None)]
        inputs = PersistInputs(
            analysis=analysis,
            memories=memories,
            embeddings=np.zeros((2, 4)),
            cluster_labels=np.array([0, 9]),
            coords_2d=np.array([[0.0, 0.0], [1.0, 1.0]]),
            cluster_results=cluster_results,
            silhouette=0.0,
            size_variance=0.0,
            outlier_ratio=0.0,
            window_from=None,
            window_to=None,
        )
        await persist_results(
            db_session,
            inputs=inputs,
            user_id="rep_user",
            workspace_id=str(fixture_workspace_id),
            context_id=str(fixture_context_id),
        )
        await db_session.flush()
        assignments = (
            (
                await db_session.execute(
                    select(MemoryAnalysisAssignment).where(
                        MemoryAnalysisAssignment.analysis_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        # Only memory 0 (cluster 0) got an assignment; memory 1 (cluster 9) skipped.
        assert len(assignments) == 1
        assert assignments[0].memory_id == mems[0].id


class TestPersistFailure:
    """``persist_failure``: mark run failed, with cancel guard + truncation."""

    async def test_marks_run_failed(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """Happy path: status→failed, finished_at set, error recorded."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        await persist_failure(db_session, analysis=analysis, error_message="boom")
        assert analysis.status == "failed"
        assert analysis.finished_at is not None
        assert analysis.error == "boom"

    async def test_error_message_truncated_to_max(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """An over-long error is truncated to _ERROR_FIELD_MAX_CHARS (8000)."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        big = "x" * 20000
        await persist_failure(db_session, analysis=analysis, error_message=big)
        assert analysis.status == "failed"
        assert len(analysis.error) == 8000
        assert analysis.error == "x" * 8000

    async def test_skips_when_cancelled(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        """A concurrently-cancelled run is not flipped to failed."""
        analysis = await _make_analysis(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            status="cancelled",
            cancellation_reason="user",
        )
        await persist_failure(db_session, analysis=analysis, error_message="late error")
        await db_session.refresh(analysis)
        assert analysis.status == "cancelled"
        assert analysis.error is None
        assert analysis.cancellation_reason == "user"
