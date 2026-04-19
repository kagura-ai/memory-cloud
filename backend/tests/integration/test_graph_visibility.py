"""Graph read visibility tests (Issue #383).

Exercises ``/graph/stats`` and ``/graph/data`` against a real database to
verify that visibility is driven by ``Context.is_private`` +
``PermissionService.check_workspace_access`` rather than the pre-#383
hardcoded ``user_id == caller`` filter.

6-way matrix (the axis that gate1 review pinned as load-bearing):

    {shared | private context} × {owner | same-workspace member | different-workspace user}

Plus the cross-workspace-probe regression from PR #391 (multi-workspace
member must not leak edges across tenant boundaries).

These cases are expressed as separate test functions (one per outcome
class) rather than a parametric sweep so a failure clearly identifies
which visibility contract regressed.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import require_session_auth
from db.base import get_db
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory, NeuralMemoryEdge


def _make_fresh_session_override(engine):
    """Yield a fresh AsyncSession per request (pattern from test_resource_cross_workspace)."""
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    return override_get_db


def _session_user(user_id: str, workspace_id) -> dict:
    return {
        "user_id": user_id,
        "email": f"{user_id}@test.com",
        "role": "user",
        "current_workspace_id": workspace_id,
        "workspace_role": "member",
    }


@pytest_asyncio.fixture
async def visibility_scenario(async_engine, db_session):
    """Set up the 6-way matrix fixture.

    Workspace A:
        - owner_a (role=owner), member_a (role=member)
        - ctx_shared (is_private=False): edges created by both owner_a and member_a
        - ctx_private (is_private=True, created_by=owner_a): edges created by owner_a

    Workspace B:
        - outsider_b (role=owner) — not a member of workspace A

    Two edges per creator per context so ``total_edges`` distinguishes
    "all" vs "owner's only" vs "empty" outcomes unambiguously.
    """
    owner_a_id = f"owner_a_{uuid4().hex[:8]}"
    member_a_id = f"member_a_{uuid4().hex[:8]}"
    outsider_b_id = f"outsider_b_{uuid4().hex[:8]}"

    ws_a = Workspace(
        id=uuid4(),
        name=f"ws-a-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_a_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ws_b = Workspace(
        id=uuid4(),
        name=f"ws-b-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=outsider_b_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )

    ctx_shared = Context(
        id=uuid4(),
        workspace_id=ws_a.id,
        name=f"ctx-shared-{uuid4().hex[:8]}",
        created_by=owner_a_id,
        is_private=False,
    )
    ctx_private = Context(
        id=uuid4(),
        workspace_id=ws_a.id,
        name=f"ctx-private-{uuid4().hex[:8]}",
        created_by=owner_a_id,
        is_private=True,
    )

    # Helper to mint a memory + return its id
    def _mk_memory(user_id: str, ctx: Context) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=user_id,
            workspace_id=ws_a.id,
            context_id=ctx.id,
            summary=f"mem-{uuid4().hex[:6]}",
            content="x",
            type="note",
            client="test",
        )

    # 2 memories per (creator, context) pair → 1 edge between them
    mem_owner_shared_src = _mk_memory(owner_a_id, ctx_shared)
    mem_owner_shared_dst = _mk_memory(owner_a_id, ctx_shared)
    mem_member_shared_src = _mk_memory(member_a_id, ctx_shared)
    mem_member_shared_dst = _mk_memory(member_a_id, ctx_shared)
    mem_owner_private_src = _mk_memory(owner_a_id, ctx_private)
    mem_owner_private_dst = _mk_memory(owner_a_id, ctx_private)

    def _mk_edge(user_id: str, ctx: Context, src: Memory, dst: Memory) -> NeuralMemoryEdge:
        return NeuralMemoryEdge(
            user_id=user_id,
            src_id=src.id,
            dst_id=dst.id,
            workspace_id=ws_a.id,
            context_id=ctx.id,
            edge_type="neural_association",
            weight=1.0,
            confidence=1.0,
        )

    edge_owner_shared = _mk_edge(owner_a_id, ctx_shared, mem_owner_shared_src, mem_owner_shared_dst)
    edge_member_shared = _mk_edge(
        member_a_id, ctx_shared, mem_member_shared_src, mem_member_shared_dst
    )
    edge_owner_private = _mk_edge(
        owner_a_id, ctx_private, mem_owner_private_src, mem_owner_private_dst
    )

    # Flush in dependency order (workspaces → members → contexts → memories → edges).
    # SQLAlchemy's UoW heuristic doesn't reliably topo-sort inserts when the
    # ORM-side ``relationship()`` is absent (Context has only a raw FK column,
    # no back-populating relationship), so the DB can see contexts before their
    # workspace rows unless we flush the parents first.
    db_session.add_all([ws_a, ws_b])
    await db_session.flush()

    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_a.id, user_id=owner_a_id, role="owner"),
            WorkspaceMember(workspace_id=ws_a.id, user_id=member_a_id, role="member"),
            WorkspaceMember(workspace_id=ws_b.id, user_id=outsider_b_id, role="owner"),
        ]
    )
    await db_session.flush()

    db_session.add_all([ctx_shared, ctx_private])
    await db_session.flush()

    db_session.add_all(
        [
            mem_owner_shared_src,
            mem_owner_shared_dst,
            mem_member_shared_src,
            mem_member_shared_dst,
            mem_owner_private_src,
            mem_owner_private_dst,
        ]
    )
    await db_session.flush()

    db_session.add_all([edge_owner_shared, edge_member_shared, edge_owner_private])
    await db_session.commit()

    app.dependency_overrides[get_db] = _make_fresh_session_override(async_engine)

    scenario = {
        "owner_a_id": owner_a_id,
        "member_a_id": member_a_id,
        "outsider_b_id": outsider_b_id,
        "ws_a_id": ws_a.id,
        "ws_b_id": ws_b.id,
        "ctx_shared_id": ctx_shared.id,
        "ctx_private_id": ctx_private.id,
        "edge_ids": [
            edge_owner_shared.id,
            edge_member_shared.id,
            edge_owner_private.id,
        ],
        "memory_ids": [
            mem_owner_shared_src.id,
            mem_owner_shared_dst.id,
            mem_member_shared_src.id,
            mem_member_shared_dst.id,
            mem_owner_private_src.id,
            mem_owner_private_dst.id,
        ],
    }

    yield scenario

    app.dependency_overrides.clear()

    # Teardown — order matters due to FKs (edges → memories → contexts → workspaces).
    try:
        await db_session.execute(
            NeuralMemoryEdge.__table__.delete().where(NeuralMemoryEdge.id.in_(scenario["edge_ids"]))
        )
        await db_session.execute(
            Memory.__table__.delete().where(Memory.id.in_(scenario["memory_ids"]))
        )
        await db_session.execute(
            Context.__table__.delete().where(Context.id.in_([ctx_shared.id, ctx_private.id]))
        )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(
                WorkspaceMember.workspace_id.in_([ws_a.id, ws_b.id])
            )
        )
        await db_session.delete(ws_a)
        await db_session.delete(ws_b)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


def _as(user_id: str, workspace_id):
    """Install a require_session_auth override returning the given user."""

    async def override():
        return _session_user(user_id, workspace_id)

    app.dependency_overrides[require_session_auth] = override


# ============================================================================
# Shared context × 3 caller roles
# ============================================================================


@pytest.mark.asyncio
async def test_shared_context_owner_sees_all_edges(visibility_scenario):
    """Owner of a shared context sees edges authored by OTHER workspace members.

    Pre-#383 this returned only owner-authored edges (count 1). Post-#383 the
    count must include member_a's edge as well (count 2).
    """
    _as(visibility_scenario["owner_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_shared_id"])},
        )

    assert response.status_code == 200, response.text
    assert response.json()["stats"]["total_edges"] == 2, (
        "Owner of shared context must see all workspace members' edges, not only their own"
    )


@pytest.mark.asyncio
async def test_shared_context_member_sees_all_edges(visibility_scenario):
    """Non-owner workspace member sees the same edges as the owner for a shared context.

    This is the regression target the bug report pinpointed — members were
    previously seeing an empty/near-empty graph even though they had legitimate
    read access through ``PermissionService.can_access_memory``.
    """
    _as(visibility_scenario["member_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_shared_id"])},
        )

    assert response.status_code == 200, response.text
    assert response.json()["stats"]["total_edges"] == 2, (
        "Workspace member must see the same edges as the owner for a shared context"
    )


@pytest.mark.asyncio
async def test_shared_context_cross_workspace_probe_returns_404(visibility_scenario):
    """Cross-workspace probe must be CWE-639 uniform-disclosure 404, not 403/empty.

    outsider_b knows the UUID of ctx_shared in workspace A (hypothetical leak)
    but is not a member of workspace A. They must get 404, indistinguishable
    from "context does not exist at all".
    """
    _as(visibility_scenario["outsider_b_id"], visibility_scenario["ws_b_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_shared_id"])},
        )

    assert response.status_code == 404, response.text


# ============================================================================
# Private context × 3 caller roles
# ============================================================================


@pytest.mark.asyncio
async def test_private_context_creator_sees_own_edges(visibility_scenario):
    """Creator of a private context sees their own edges (full subgraph)."""
    _as(visibility_scenario["owner_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_private_id"])},
        )

    assert response.status_code == 200, response.text
    assert response.json()["stats"]["total_edges"] == 1, (
        "Creator of private context must see their own edges"
    )


@pytest.mark.asyncio
async def test_private_context_non_creator_member_returns_404(visibility_scenario):
    """Workspace member who is NOT the private-context creator gets uniform 404.

    Matches the ``can_access_memory`` rule ("private → only creator can
    access") and the rest-of-API convention established by
    ``PermissionService.check_context_access``. Copilot catch on PR #394
    loop 2: returning an empty 200 leaked "a private context with this ID
    exists, and you are not its creator" — the 404 hides that differential.
    """
    _as(visibility_scenario["member_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_private_id"])},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_private_context_cross_workspace_probe_returns_404(visibility_scenario):
    """Outsider probing a private context UUID gets CWE-639 uniform 404."""
    _as(visibility_scenario["outsider_b_id"], visibility_scenario["ws_b_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(visibility_scenario["ctx_private_id"])},
        )

    assert response.status_code == 404, response.text


# ============================================================================
# /graph/data parity — the behavior must match /graph/stats
# ============================================================================


@pytest.mark.asyncio
async def test_graph_data_shared_member_sees_all_edges(visibility_scenario):
    """``/graph/data`` honors the same visibility rule as ``/graph/stats``."""
    _as(visibility_scenario["member_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/data",
            params={"context_id": str(visibility_scenario["ctx_shared_id"])},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stats"]["total_edges"] == 2, (
        "/graph/data must surface the same shared-context edges as /graph/stats"
    )


@pytest.mark.asyncio
async def test_graph_data_cross_workspace_returns_404(visibility_scenario):
    """``/graph/data`` uniform 404 on cross-workspace probes."""
    _as(visibility_scenario["outsider_b_id"], visibility_scenario["ws_b_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/data",
            params={"context_id": str(visibility_scenario["ctx_shared_id"])},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_soft_deleted_context_returns_404(visibility_scenario, db_session):
    """Soft-deleted context surfaces as 404 even for a legitimate workspace member.

    ``resolve_context_for_workspace_read`` mirrors ``resolve_resource_by_slug``'s
    ``Context.deleted_at.is_(None)`` filter — stale graph data from a deleted
    context must not be reachable.
    """
    from models.auth import Context
    from utils.datetime import utcnow

    ctx_id = visibility_scenario["ctx_shared_id"]
    await db_session.execute(
        Context.__table__.update()
        .where(Context.id == ctx_id)
        .values(deleted_at=utcnow().replace(tzinfo=None))
    )
    await db_session.commit()

    _as(visibility_scenario["owner_a_id"], visibility_scenario["ws_a_id"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/graph/stats",
            params={"context_id": str(ctx_id)},
        )

    assert response.status_code == 404, response.text
