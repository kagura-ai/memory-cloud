"""Integration test for declared_link protection in upsert (#457).

NeuralEdgeRepository.create_or_update_edge with protect_declared_link=True
must keep an existing edge_type="declared_link" pinned even when the
incoming upsert tries to set edge_type="neural_association" (e.g. Hebbian
co-activation flowing through GraphService.add_edge).

The smoke-test on v0.14.0 (2026-04-26 prod) reproduced the unprotected
case 100% — user-declared links got retyped to neural_association as
soon as Hebbian fired on either endpoint.

The protected path must still bump weight/confidence/last_updated, so
co-activation continues to strengthen the user-declared link.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory
from repositories.neural_edge import NeuralEdgeRepository


@pytest_asyncio.fixture
async def declared_link_scenario(db_session: AsyncSession):
    """Workspace + context + two memories ready for an edge between them."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )

    def _mk_memory() -> Memory:
        return Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"mem-{uuid4().hex[:6]}",
            content="x",
            type="note",
            client="test",
        )

    mem_a = _mk_memory()
    mem_b = _mk_memory()

    db_session.add(ws)
    await db_session.flush()
    db_session.add(ctx)
    await db_session.flush()
    db_session.add_all([mem_a, mem_b])
    await db_session.flush()

    yield {
        "owner_id": owner_id,
        "ws_id": ws.id,
        "ctx_id": ctx.id,
        "mem_a": mem_a,
        "mem_b": mem_b,
    }

    await db_session.rollback()


@pytest.mark.asyncio
async def test_protect_declared_link_preserves_edge_type(
    db_session: AsyncSession, declared_link_scenario
):
    """Hebbian-style retyping must not clobber declared_link edge_type.

    Scenario: a `declared_link` edge exists (user-declared). Hebbian
    co-activation later fires and tries to upsert the same (src, dst)
    with edge_type="neural_association" via GraphService.add_edge,
    which passes protect_declared_link=True. The edge_type must stay
    "declared_link"; weight/last_updated may update.
    """
    repo = NeuralEdgeRepository(db_session)
    s = declared_link_scenario

    declared = await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="declared_link",
        weight=1.0,
        confidence=1.0,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
    )
    assert declared.edge_type == "declared_link"
    assert declared.weight == 1.0
    initial_last_updated = declared.last_updated

    retyped = await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="neural_association",
        weight=1.03,
        confidence=1.0,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        protect_declared_link=True,
    )

    assert retyped.edge_type == "declared_link", (
        "protect_declared_link=True must pin the existing declared_link type"
    )
    assert retyped.weight == 1.03, (
        "weight must still update so co-activation continues to strengthen the link"
    )
    assert retyped.last_updated >= initial_last_updated


@pytest.mark.asyncio
async def test_unprotected_upsert_still_allows_user_driven_retype(
    db_session: AsyncSession, declared_link_scenario
):
    """User-driven update_edge (protect_declared_link=False) keeps the existing contract.

    The MCP `update_edge` handler calls create_or_update_edge directly
    (not via GraphService) with the default protect=False, so an
    explicit user-initiated edge_type change must still work — this
    is how a user downgrades a declared_link they regret.
    """
    repo = NeuralEdgeRepository(db_session)
    s = declared_link_scenario

    await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="declared_link",
        weight=1.0,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
    )

    retyped = await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="neural_association",
        weight=0.5,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        # protect_declared_link defaults to False — user-driven path
    )

    assert retyped.edge_type == "neural_association", (
        "Without protection, user-driven update_edge can change the type explicitly"
    )
    assert retyped.weight == 0.5


@pytest.mark.asyncio
async def test_protect_does_not_block_non_declared_retype(
    db_session: AsyncSession, declared_link_scenario
):
    """protect_declared_link only pins existing declared_link rows.

    A neural_association → related_to retype must still succeed when
    protect_declared_link=True, because the existing edge_type is not
    declared_link. This guards against an over-broad WHERE clause.
    """
    repo = NeuralEdgeRepository(db_session)
    s = declared_link_scenario

    await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="neural_association",
        weight=0.5,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
    )

    retyped = await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_a"].id,
        dst_id=s["mem_b"].id,
        edge_type="related_to",
        weight=0.7,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_id"]),
        protect_declared_link=True,
    )

    assert retyped.edge_type == "related_to"
    assert retyped.weight == 0.7
