"""#1834: graph reads follow the identity-link set inside a private context.

Recall and the memory list already return a linked account's memories there;
graph stats/data, ``list_edges`` and ``explore`` filtered edges by the caller
alone, so a seed written by the linked account looked ``seed_not_in_graph``.
Writes, deletes and Sleep stay per account.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from models.auth import Context, IdentityLink, User, Workspace
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig
from repositories.neural_edge import NeuralEdgeRepository
from services.graph_service import GraphService
from services.identity_link_service import linked_user_ids
from services.memory_health_service import MemoryHealthService
from utils.datetime import utcnow


async def _private_scope(db, owner: str) -> tuple[UUID, UUID]:
    ws = Workspace(
        id=uuid4(),
        name=f"il-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"il-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=True,
    )
    db.add(ctx)
    await db.flush()
    return ws.id, ctx.id


async def _link(db, *accounts: str) -> None:
    """Make ``accounts`` one person. identity_links.user_id is a FK onto users."""
    for account in accounts:
        db.add(User(email=f"{account}@test.example", user_id=account, role="user"))
    await db.flush()
    group = uuid4()
    for account in accounts:
        db.add(IdentityLink(group_id=group, user_id=account, linked_by=accounts[0]))
    await db.flush()


async def _node(db, user_id: str, workspace_id: UUID, context_id: UUID) -> UUID:
    mem = Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary="node summary",
        content="node content",
        type="note",
        importance=0.5,
        confidence=1.0,
        client="pytest",
        source="manual",
        created_at=utcnow(),
        embedding_status="success",
    )
    db.add(mem)
    await db.flush()
    return mem.id


def _graph(db, user_id: str, ws: UUID, ctx: UUID, owner_ids=None) -> GraphService:
    return GraphService(
        user_id=user_id, db=db, workspace_id=str(ws), context_id=str(ctx), owner_ids=owner_ids
    )


@pytest.fixture
async def linked_graph(db_session):
    """A private context owned by A; B is linked to A and wrote one edge; C is
    an unlinked third account that also wrote one edge there."""
    a, b, c = f"a-{uuid4().hex[:6]}", f"b-{uuid4().hex[:6]}", f"c-{uuid4().hex[:6]}"
    ws, ctx = await _private_scope(db_session, a)
    await _link(db_session, a, b)
    b_src, b_dst = await _node(db_session, b, ws, ctx), await _node(db_session, b, ws, ctx)
    await _graph(db_session, b, ws, ctx).add_edge(b_src, b_dst, weight=1.0)
    c_src, c_dst = await _node(db_session, c, ws, ctx), await _node(db_session, c, ws, ctx)
    await _graph(db_session, c, ws, ctx).add_edge(c_src, c_dst, weight=1.0)
    owners = await linked_user_ids(db_session, a)
    assert owners == frozenset({a, b})
    return {
        "a": a,
        "b": b,
        "c": c,
        "ws": ws,
        "ctx": ctx,
        "owners": owners,
        "b_edge": (b_src, b_dst),
        "c_edge": (c_src, c_dst),
    }


class TestReadsFollowTheLinkSet:
    @pytest.mark.asyncio
    async def test_the_linked_accounts_edges_are_in_the_graph(self, db_session, linked_graph):
        g = linked_graph
        graph = _graph(db_session, g["a"], g["ws"], g["ctx"], owner_ids=g["owners"])

        # explore's seed check: B's node is in A's graph now.
        assert await graph.has_node(g["b_edge"][0]) is True
        # ... but an unlinked account's node is not.
        assert await graph.has_node(g["c_edge"][0]) is False

        stats = await graph.stats()
        assert stats["total_edges"] == 1
        assert stats["total_nodes"] == 2  # B's two nodes, not C's

        edges = await graph.edge_repo.get_all_edges(
            user_id=g["owners"], workspace_id=str(g["ws"]), context_id=str(g["ctx"])
        )
        assert {(e.src_id, e.dst_id) for e in edges} == {g["b_edge"]}

    @pytest.mark.asyncio
    async def test_without_the_set_the_caller_sees_only_its_own(self, db_session, linked_graph):
        """The per-account default (Sleep, consolidation, shared contexts)."""
        g = linked_graph
        graph = _graph(db_session, g["a"], g["ws"], g["ctx"])

        assert await graph.has_node(g["b_edge"][0]) is False
        assert (await graph.stats())["total_edges"] == 0

    @pytest.mark.asyncio
    async def test_the_spread_crosses_the_linked_accounts_edges(self, db_session, linked_graph):
        """explore's activation spread, seeded on B's node, reaches B's neighbour
        through A's graph service — and never touches C's edge."""
        g = linked_graph
        graph = _graph(db_session, g["a"], g["ws"], g["ctx"], owner_ids=g["owners"])
        spreader = ActivationSpreader(graph, NeuralMemoryConfig(spread_hops=1))

        activated = await spreader.spread(
            seed_activations={str(g["b_edge"][0]): 1.0}, user_id=graph.read_owner
        )

        reached = {str(a.node_id) for a in activated}
        assert str(g["b_edge"][1]) in reached
        assert str(g["c_edge"][1]) not in reached


class TestWritesStayPerAccount:
    @pytest.mark.asyncio
    async def test_a_linked_account_cannot_delete_the_others_edge(self, db_session, linked_graph):
        g = linked_graph
        repo = NeuralEdgeRepository(db_session)
        src, dst = g["b_edge"]

        assert (
            await repo.delete_edge(
                g["a"], src, dst, workspace_id=str(g["ws"]), context_id=str(g["ctx"])
            )
            is False
        )
        assert (
            await repo.delete_edge(
                g["b"], src, dst, workspace_id=str(g["ws"]), context_id=str(g["ctx"])
            )
            is True
        )


class TestMemoryHealthCoversTheSet:
    @pytest.mark.asyncio
    async def test_owned_contexts_include_the_linked_accounts_private_ones(self, db_session):
        a, b, c = f"a-{uuid4().hex[:6]}", f"b-{uuid4().hex[:6]}", f"c-{uuid4().hex[:6]}"
        await _link(db_session, a, b)
        _, mine = await _private_scope(db_session, a)
        _, linked = await _private_scope(db_session, b)
        _, other = await _private_scope(db_session, c)
        # A link grants ownership of PRIVATE contexts, not membership: B's
        # shared context stays out of A's report.
        ws_shared, shared = await _private_scope(db_session, b)
        (await db_session.get(Context, shared)).is_private = False
        await db_session.flush()

        owned = {cid for cid, _ in await MemoryHealthService(db_session)._fetch_owned_contexts(a)}

        assert mine in owned and linked in owned
        assert other not in owned and shared not in owned

    @pytest.mark.asyncio
    async def test_signals_in_a_covered_context_count_the_link_sets_rows(self, db_session):
        """The same scoping for every signal: B's merge losers in B's private
        context show up in A's report; B's rows elsewhere, and an unlinked
        member's rows inside A's own shared context, do not."""
        a, b, m = f"a-{uuid4().hex[:6]}", f"b-{uuid4().hex[:6]}", f"m-{uuid4().hex[:6]}"
        await _link(db_session, a, b)
        ws, linked = await _private_scope(db_session, b)
        _, elsewhere = await _private_scope(db_session, b)
        (await db_session.get(Context, elsewhere)).is_private = False
        ws_a, mine_shared = await _private_scope(db_session, a)
        (await db_session.get(Context, mine_shared)).is_private = False
        await db_session.flush()

        def loser(user_id: str, workspace_id, ctx) -> Memory:
            return Memory(
                id=uuid4(),
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=ctx,
                summary="merge loser",
                content="c",
                type="note",
                client="pytest",
                embedding_status="success",
                deleted_by=DELETED_BY_SLEEP_MERGE,
                deleted_at=utcnow(),
            )

        db_session.add_all(
            [loser(b, ws, linked), loser(b, ws, elsewhere), loser(m, ws_a, mine_shared)]
        )
        await db_session.flush()
        svc = MemoryHealthService(db_session)
        owned_ids = frozenset(cid for cid, _ in await svc._fetch_owned_contexts(a))
        assert {linked, mine_shared} <= owned_ids

        signals = await svc._fetch_signals(a, owned_ids=owned_ids)
        backlogs = signals["backlogs"]

        assert backlogs[linked]["count"] == 1
        assert elsewhere not in backlogs
        assert mine_shared not in backlogs  # M is a member, not a linked owner
