"""Schema integrity tests for Memory Broadlistening tables (#494).

Covers the acceptance criteria from issue #494:

- alembic upgrade head clean (forward then rollback) is exercised by the
  generic ``test_alembic_migrations.TestAlembicMigrations`` suite; this
  module focuses on the post-upgrade *behavior* of the new schema.
- CHECK constraint on ``memory_analyses.status`` rejects invalid values.
- CHECK constraint on ``memory_analyses.paid_by`` rejects values outside
  the ``byok | platform`` set.
- ``memory_analysis_assignments`` PK is composite ``(analysis_id, memory_id)``.
- FK from ``workspaces.analysis_default_model_id`` to ``llm_pricing(id)``
  is enforced.
- ``Workspace.effective_analysis_runs_per_day`` returns
  ``tier_base + addon_analysis_bonus`` (FREE=0, BASIC=0, PRO=3 + bonus).
- ``addon_calculator_service`` recognizes the ``extra_analysis_runs`` SKU
  and writes ``addon_analysis_bonus``.

The session-scoped fixtures from ``tests/conftest.py`` create the schema
via ``Base.metadata.create_all`` rather than ``alembic upgrade``; that
keeps these tests fast and isolated from migration version state, while
``test_alembic_migrations`` exercises the migration paths separately.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.analysis import (
    MemoryAnalysis,
    MemoryAnalysisAssignment,
    MemoryAnalysisCluster,
)
from models.auth import Context, Workspace
from models.llm_pricing import LLMPricing
from models.memory import Memory
from models.resource import WorkspaceAddon


async def _seed_workspace_context_pricing(
    db: AsyncSession,
    *,
    plan: str = "pro",
) -> tuple[Workspace, Context, LLMPricing, str]:
    """Insert workspace + context + a unique llm_pricing row; return them.

    Flush between workspace and context: ``contexts.workspace_id`` FK
    has no ``relationship()`` to drive insert order, so a single flush
    can attempt the child row first.
    """
    owner_id = f"user-{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name=plan,
        owner_user_id=owner_id,
        daily_api_limit=10000,
        weekly_api_limit=50000,
    )
    db.add(ws)
    await db.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
    )
    db.add(ctx)

    # Unique model name per call avoids collisions on
    # uq_llm_pricing_lookup_key when other tests in the same DB session
    # have inserted pricing rows.
    pricing = LLMPricing(
        provider="google",
        model=f"gemini-test-{uuid4().hex[:8]}",
        unit_type="input_tokens",
        effective_from=datetime(2026, 1, 1),
        price_per_unit=Decimal("0.075"),
        currency="USD",
        unit_denominator=1_000_000,
        context_min_tokens=0,
    )
    db.add(pricing)

    await db.flush()
    return ws, ctx, pricing, owner_id


def _build_analysis(
    ws: Workspace,
    ctx: Context,
    pricing: LLMPricing,
    owner_id: str,
    *,
    status: str = "running",
    paid_by: str = "byok",
) -> MemoryAnalysis:
    """Construct a minimal ``MemoryAnalysis`` ORM object (no DB write)."""
    return MemoryAnalysis(
        id=uuid4(),
        workspace_id=ws.id,
        context_id=ctx.id,
        status=status,
        triggered_by=owner_id,
        model_id=pricing.id,
        model_snapshot={},
        embedding_model="text-embedding-3-small",
        params={},
        input_count=100,
        paid_by=paid_by,
    )


# ---------------------------------------------------------------------------
# CHECK constraints on memory_analyses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_check_rejects_invalid_value(db_session: AsyncSession) -> None:
    """``status`` outside the allowed set raises IntegrityError."""
    ws, ctx, pricing, owner_id = await _seed_workspace_context_pricing(db_session)

    db_session.add(_build_analysis(ws, ctx, pricing, owner_id, status="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_status_check_accepts_each_valid_value(
    db_session: AsyncSession,
) -> None:
    """All four allowed status values insert cleanly."""
    ws, ctx, pricing, owner_id = await _seed_workspace_context_pricing(db_session)

    for status in ("running", "succeeded", "failed", "cancelled"):
        db_session.add(_build_analysis(ws, ctx, pricing, owner_id, status=status))
    await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_paid_by_check_rejects_invalid_value(
    db_session: AsyncSession,
) -> None:
    """``paid_by`` outside ``{byok, platform}`` raises IntegrityError."""
    ws, ctx, pricing, owner_id = await _seed_workspace_context_pricing(db_session)

    db_session.add(_build_analysis(ws, ctx, pricing, owner_id, paid_by="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# FK + composite PK + label_confidence CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_default_model_fk_enforced(
    db_session: AsyncSession,
) -> None:
    """Setting ``analysis_default_model_id`` to a non-existent llm_pricing.id fails."""
    ws, _, pricing, _ = await _seed_workspace_context_pricing(db_session)

    # ``pricing.id + 1`` is provably non-existent — the just-flushed pricing
    # row holds the highest sequence value and we have not inserted further
    # rows. Avoids a hard-coded sentinel that could collide on a busy
    # shared test DB where the BIGINT sequence has advanced past it.
    ws.analysis_default_model_id = pricing.id + 1
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_workspace_default_model_set_null_on_pricing_delete(
    db_session: AsyncSession,
) -> None:
    """Deleting the pricing row clears ``analysis_default_model_id`` to NULL.

    Verifies the ``ON DELETE SET NULL`` semantics — a pricing-row cleanup
    must not cascade-delete the workspace, only clear its model selection.
    """
    ws, _, pricing, _ = await _seed_workspace_context_pricing(db_session)

    ws.analysis_default_model_id = pricing.id
    await db_session.flush()
    assert ws.analysis_default_model_id == pricing.id

    await db_session.delete(pricing)
    await db_session.flush()
    await db_session.refresh(ws)

    assert ws.analysis_default_model_id is None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_assignment_composite_pk_blocks_duplicate(
    db_session: AsyncSession,
) -> None:
    """Inserting the same (analysis_id, memory_id) twice raises IntegrityError."""
    ws, ctx, pricing, owner_id = await _seed_workspace_context_pricing(db_session)
    analysis = _build_analysis(ws, ctx, pricing, owner_id)
    db_session.add(analysis)
    await db_session.flush()

    cluster = MemoryAnalysisCluster(
        id=uuid4(),
        analysis_id=analysis.id,
        cluster_index=0,
        label="cluster-0",
        count=1,
        centroid_2d=[0.0, 0.0],
        representative_memory_ids=[],
        property_stats={},
        label_confidence=0.9,
    )
    db_session.add(cluster)

    memory = Memory(
        id=uuid4(),
        workspace_id=ws.id,
        context_id=ctx.id,
        user_id=owner_id,
        summary="test",
        content="test",
        type="note",
        client="test",
    )
    db_session.add(memory)
    await db_session.flush()

    db_session.add(
        MemoryAnalysisAssignment(
            analysis_id=analysis.id,
            memory_id=memory.id,
            cluster_id=cluster.id,
            x=0.0,
            y=0.0,
        )
    )
    await db_session.flush()

    db_session.add(
        MemoryAnalysisAssignment(
            analysis_id=analysis.id,
            memory_id=memory.id,
            cluster_id=cluster.id,
            x=1.0,
            y=1.0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_label_confidence_check_rejects_out_of_range(
    db_session: AsyncSession,
) -> None:
    """``label_confidence`` outside [0, 1] raises IntegrityError."""
    ws, ctx, pricing, owner_id = await _seed_workspace_context_pricing(db_session)
    analysis = _build_analysis(ws, ctx, pricing, owner_id)
    db_session.add(analysis)
    await db_session.flush()

    db_session.add(
        MemoryAnalysisCluster(
            id=uuid4(),
            analysis_id=analysis.id,
            cluster_index=0,
            label="bad",
            count=1,
            centroid_2d=[0.0, 0.0],
            representative_memory_ids=[],
            property_stats={},
            label_confidence=1.5,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# addon_analysis_bonus column + WorkspaceAddon CHECK extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_addon_analysis_bonus_default_zero(
    db_session: AsyncSession,
) -> None:
    """A newly inserted workspace has ``addon_analysis_bonus = 0`` by default."""
    ws, _, _, _ = await _seed_workspace_context_pricing(db_session)
    await db_session.refresh(ws)
    assert ws.addon_analysis_bonus == 0


@pytest.mark.asyncio
async def test_workspace_addon_check_accepts_extra_analysis_runs(
    db_session: AsyncSession,
) -> None:
    """The CHECK extension lets a Stripe webhook insert the new SKU."""
    ws, _, _, owner_id = await _seed_workspace_context_pricing(db_session)
    db_session.add(
        WorkspaceAddon(
            workspace_id=ws.id,
            addon_type="extra_analysis_runs",
            quantity=1,
            created_by=owner_id,
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_workspace_addon_check_still_rejects_unknown(
    db_session: AsyncSession,
) -> None:
    """Unknown addon_type values still fail (constraint not weakened)."""
    ws, _, _, owner_id = await _seed_workspace_context_pricing(db_session)
    db_session.add(
        WorkspaceAddon(
            workspace_id=ws.id,
            addon_type="extra_bogus_quota",
            quantity=1,
            created_by=owner_id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Workspace.effective_analysis_runs_per_day math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "bonus", "expected"),
    [
        ("pro", 0, 3),  # PRO base
        ("pro", 5, 8),  # PRO + addon offset
        ("free", 0, 0),  # FREE base
        ("basic", 0, 0),  # BASIC base
        ("free", 2, 0),  # FREE + addon -> 0 (_zero_floor #569 defense: zero base overrides addon)
    ],
)
def test_effective_analysis_runs(plan: str, bonus: int, expected: int) -> None:
    """``effective_analysis_runs_per_day`` = plan-tier base + addon bonus."""
    ws = Workspace(plan_name=plan, addon_analysis_bonus=bonus)
    assert ws.effective_analysis_runs_per_day == expected


# ---------------------------------------------------------------------------
# AddonCalculatorService.extra_analysis_runs SKU
# ---------------------------------------------------------------------------


def test_addon_unit_values_includes_extra_analysis_runs() -> None:
    """The Stripe SKU is registered with a +1 unit value."""
    from services.addon_calculator_service import ADDON_UNIT_VALUES

    assert ADDON_UNIT_VALUES["extra_analysis_runs"] == 1
