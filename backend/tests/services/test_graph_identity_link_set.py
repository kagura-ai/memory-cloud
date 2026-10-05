"""#1834: graph reads follow the identity-link set inside a private context.

Recall and the memory list already return a linked account's memories there;
graph stats/data, ``list_edges`` and ``explore`` filtered edges by the caller
alone, so a seed written by the linked account looked ``seed_not_in_graph``.
Edge writes and deletes, and the Sleep / consolidation jobs, stay per account.
The memory-health report is a read: inside a context it covers, it counts the
link set's sleep windows, usage and read attributions (#1834, #1874).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from auth.workspace_roles import WorkspaceRole
from models.auth import (
    Context,
    ContextReadAttribution,
    IdentityLink,
    UsageStats,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from models.schemas import ExploreRequest, MemoryResponse
from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig
from repositories.neural_edge import NeuralEdgeRepository
from services.graph_service import GraphService
from services.identity_link_service import linked_user_ids
from services.memory_health_service import MemoryHealthService
from services.memory_service import MemoryService
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


async def _join_as_admin(db, workspace_id: UUID, user_id: str) -> None:
    """#1874: a link widens ownership only — the health report covers a linked
    account's private context where the caller is itself a member."""
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=WorkspaceRole.ADMIN))
    await db.flush()


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


@pytest.fixture
async def parallel_graph(db_session):
    """#1867: A and B are linked and EACH owns an edge for the same pair
    (M1 -> M2) in A's private context — what recall's per-account Hebbian
    write leaves behind once both accounts co-activated the pair. A also owns
    a weaker edge M1 -> M3."""
    a, b = f"a-{uuid4().hex[:6]}", f"b-{uuid4().hex[:6]}"
    ws, ctx = await _private_scope(db_session, a)
    await _link(db_session, a, b)
    m1 = await _node(db_session, a, ws, ctx)
    m2 = await _node(db_session, b, ws, ctx)
    m3 = await _node(db_session, a, ws, ctx)
    await _graph(db_session, a, ws, ctx).add_edge(m1, m2, weight=0.4)
    await _graph(db_session, b, ws, ctx).add_edge(m1, m2, weight=0.9)
    await _graph(db_session, a, ws, ctx).add_edge(m1, m3, weight=0.2)
    owners = await linked_user_ids(db_session, a)
    assert owners == frozenset({a, b})
    return {"a": a, "b": b, "ws": ws, "ctx": ctx, "owners": owners, "m": (m1, m2, m3)}


class TestParallelEdgesForOnePair:
    """#1867: ``unique_edge`` is per account, so a set-owner read can match two
    rows for one (src, dst). It reads them as ONE edge — the strongest."""

    @pytest.mark.asyncio
    async def test_get_edge_picks_the_strongest_row(self, db_session, parallel_graph):
        g = parallel_graph
        m1, m2, _ = g["m"]
        repo = NeuralEdgeRepository(db_session)
        scope = {"workspace_id": str(g["ws"]), "context_id": str(g["ctx"])}

        edge = await repo.get_edge(g["owners"], m1, m2, **scope)

        assert edge is not None
        assert edge.user_id == g["b"] and edge.weight == pytest.approx(0.9)
        # One account still reads its own row.
        assert (await repo.get_edge(g["a"], m1, m2, **scope)).weight == pytest.approx(0.4)
        assert (await repo.get_edge(g["b"], m1, m2, **scope)).weight == pytest.approx(0.9)
        assert await repo.get_edge(g["owners"], m2, m1, **scope) is None

    @pytest.mark.asyncio
    async def test_get_edge_breaks_a_weight_tie_by_the_latest_update(
        self, db_session, parallel_graph
    ):
        g = parallel_graph
        m1, m2, _ = g["m"]
        repo = NeuralEdgeRepository(db_session)
        mine = await repo.get_edge(g["a"], m1, m2)
        theirs = await repo.get_edge(g["b"], m1, m2)
        mine.weight = theirs.weight = 0.5
        theirs.last_updated = utcnow() - timedelta(days=1)
        mine.last_updated = utcnow()
        await db_session.flush()

        assert (await repo.get_edge(g["owners"], m1, m2)).user_id == g["a"]

    @pytest.mark.asyncio
    async def test_graph_service_get_edge_and_has_edge_through_owner_ids(
        self, db_session, parallel_graph
    ):
        g = parallel_graph
        m1, m2, m3 = g["m"]
        graph = _graph(db_session, g["a"], g["ws"], g["ctx"], owner_ids=g["owners"])

        edge = await graph.get_edge(m1, m2)

        assert edge is not None and edge["weight"] == pytest.approx(0.9)
        assert await graph.has_edge(str(m1), str(m2)) is True
        assert await graph.has_edge(m1, m3) is True
        assert await graph.has_edge(m2, m3) is False

    @pytest.mark.asyncio
    async def test_edge_lists_hold_the_pair_once(self, db_session, parallel_graph):
        g = parallel_graph
        m1, m2, m3 = g["m"]
        repo = NeuralEdgeRepository(db_session)
        scope = {"workspace_id": str(g["ws"]), "context_id": str(g["ctx"])}

        outgoing = await repo.get_outgoing_edges(g["owners"], m1, **scope)
        incoming = await repo.get_incoming_edges(g["owners"], m2, **scope)
        everything = await repo.get_all_edges(user_id=g["owners"], **scope)

        # Strongest row per pair, still sorted by weight descending.
        assert [(e.dst_id, e.weight) for e in outgoing] == [
            (m2, pytest.approx(0.9)),
            (m3, pytest.approx(0.2)),
        ]
        assert [(e.src_id, e.weight) for e in incoming] == [(m1, pytest.approx(0.9))]
        assert sorted((e.src_id, e.dst_id) for e in everything) == sorted([(m1, m2), (m1, m3)])
        assert {e.dst_id: e.weight for e in everything}[m2] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_limits_and_filters_apply_to_pairs(self, db_session, parallel_graph):
        """list_edges asks for ``limit + 1`` rows to learn whether more exist:
        a parallel row must not fill the page or fake a next one."""
        g = parallel_graph
        m1, m2, m3 = g["m"]
        repo = NeuralEdgeRepository(db_session)
        scope = {"workspace_id": str(g["ws"]), "context_id": str(g["ctx"])}

        page = await repo.get_outgoing_edges(g["owners"], m1, limit=2, **scope)
        assert [e.dst_id for e in page] == [m2, m3]
        top = await repo.get_outgoing_edges(g["owners"], m1, limit=1, **scope)
        assert [(e.dst_id, e.weight) for e in top] == [(m2, pytest.approx(0.9))]
        one_in = await repo.get_incoming_edges(g["owners"], m2, limit=2, **scope)
        assert len(one_in) == 1

        # min_weight filters rows before the collapse: the pair survives on
        # B's 0.9 row, A's 0.4 row and the 0.2 edge fall out.
        strong = await repo.get_outgoing_edges(g["owners"], m1, min_weight=0.5, **scope)
        assert [(e.dst_id, e.user_id) for e in strong] == [(m2, g["b"])]
        assert len(await repo.get_all_edges(user_id=g["owners"], min_weight=0.5, **scope)) == 1
        typed = await repo.get_outgoing_edges(
            g["owners"], m1, edge_types=["neural_association"], **scope
        )
        assert [e.dst_id for e in typed] == [m2, m3]
        assert await repo.get_outgoing_edges(g["owners"], m1, edge_types=["extends"], **scope) == []

    @pytest.mark.asyncio
    async def test_node_degree_counts_the_pair_once(self, db_session, parallel_graph):
        g = parallel_graph
        m1, m2, m3 = g["m"]
        repo = NeuralEdgeRepository(db_session)

        assert await repo.get_node_degree(g["owners"], m1) == (0, 2)
        assert await repo.get_node_degree(g["owners"], m2) == (1, 0)
        assert await repo.get_node_degree(g["owners"], m3) == (1, 0)
        # Per account: unchanged.
        assert await repo.get_node_degree(g["a"], m1) == (0, 2)
        assert await repo.get_node_degree(g["b"], m1) == (0, 1)

    @pytest.mark.asyncio
    async def test_the_spread_does_not_double_count_the_pair(self, db_session, parallel_graph):
        """The spread sums what reaches a node over its incoming edges, so two
        rows for M1 -> M2 would pass M2's neighbour A's and B's weight added
        up. It gets the strongest row's alone."""
        g = parallel_graph
        m1, m2, _ = g["m"]
        m4 = await _node(db_session, g["a"], g["ws"], g["ctx"])
        await _graph(db_session, g["a"], g["ws"], g["ctx"]).add_edge(m2, m4, weight=1.0)
        graph = _graph(db_session, g["a"], g["ws"], g["ctx"], owner_ids=g["owners"])
        config = NeuralMemoryConfig(spread_hops=2)

        activated = await ActivationSpreader(graph, config).spread(
            seed_activations={str(m1): 1.0}, user_id=graph.read_owner
        )

        by_node = {str(a.node_id): a.activation for a in activated}
        assert len(by_node) == len(activated)
        assert by_node[str(m2)] == pytest.approx(0.9 * config.spread_decay)
        assert by_node[str(m4)] == pytest.approx(0.9 * config.spread_decay**2)

    @pytest.mark.asyncio
    async def test_a_single_account_and_its_writes_are_unchanged(self, db_session, parallel_graph):
        g = parallel_graph
        m1, m2, _ = g["m"]
        repo = NeuralEdgeRepository(db_session)
        scope = {"workspace_id": str(g["ws"]), "context_id": str(g["ctx"])}

        mine = await repo.get_outgoing_edges(g["a"], m1, **scope)
        assert [(e.dst_id, e.user_id) for e in mine] == [(m2, g["a"]), (g["m"][2], g["a"])]

        # A deletes ITS row for the pair; B's row is still there, and is what
        # the link set reads afterwards.
        assert await repo.delete_edge(g["a"], m1, m2, **scope) is True
        left = await repo.get_edge(g["owners"], m1, m2, **scope)
        assert left is not None and left.user_id == g["b"]
        assert await repo.get_edge(g["a"], m1, m2, **scope) is None


class TestGraphDataRouteWithParallelEdges:
    @pytest.mark.asyncio
    async def test_the_pair_is_one_edge_with_single_degrees(self, db_session, parallel_graph):
        """#1867: ``GET /graph/data`` lists the shared pair once and derives
        node degrees from pairs, not from each account's row."""
        from api.routes.graph import get_graph_data

        g = parallel_graph
        m1, m2, m3 = g["m"]
        context = await db_session.get(Context, g["ctx"])

        with patch(
            "api.routes.graph.PermissionService.resolve_context_for_workspace_read",
            new=AsyncMock(return_value=context),
        ):
            data = await get_graph_data(
                user={"user_id": g["a"]},
                db=db_session,
                context_id=g["ctx"],
                limit_nodes=100,
                min_weight=0.0,
                memory_types=None,
            )

        assert sorted((e.source, e.target) for e in data.edges) == sorted(
            [(str(m1), str(m2)), (str(m1), str(m3))]
        )
        assert {e.target: e.weight for e in data.edges}[str(m2)] == pytest.approx(0.9)
        assert {n.id: n.degree for n in data.nodes} == {str(m1): 2, str(m2): 1, str(m3): 1}
        assert data.stats["total_edges"] == 2
        assert data.stats["filtered_edges"] == 2


class TestMemoryServiceInAPrivateContext:
    """#1867: the two ``MemoryService`` call sites that read with the link set
    (``_is_private_context`` is True, not mocked away)."""

    @pytest.mark.asyncio
    async def test_explore_returns_the_shared_pair_once(self, db_session, parallel_graph):
        g = parallel_graph
        m1, m2, m3 = g["m"]
        service = MemoryService(db_session)
        assert await service._is_private_context(g["ctx"]) is True

        with (
            # explore commits its access-stat bumps; keep the shared test DB clean.
            patch.object(db_session, "commit", new=db_session.flush),
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                new=AsyncMock(return_value=True),
            ),
        ):
            response = await service.explore(
                request=ExploreRequest(memory_id=m1, depth=1), user_id=g["a"]
            )

        related = {r.memory_id: r for r in response.related_memories}
        assert set(related) == {m2, m3}
        assert len(response.related_memories) == 2
        assert related[m2].weight == pytest.approx(0.9)
        assert related[m3].weight == pytest.approx(0.2)
        assert response.metadata["returned"] == 2

    @pytest.mark.asyncio
    async def test_recall_explore_hint_degree_counts_the_pair_once(
        self, db_session, parallel_graph
    ):
        g = parallel_graph
        m1, m2, m3 = g["m"]
        service = MemoryService(db_session)

        def result(memory_id: UUID) -> MemoryResponse:
            return MemoryResponse(
                memory_id=memory_id,
                summary="s",
                context_summary=None,
                context=None,
                type="note",
                importance=0.5,
                scope="working",
                created_at=utcnow(),
                client="pytest",
                tags=[],
            )

        seen: dict[UUID, tuple] = {}
        real_degree = NeuralEdgeRepository.get_node_degree

        async def spy(self, user_id, node_id):
            degree = await real_degree(self, user_id, node_id)
            seen[node_id] = (user_id, degree)
            return degree

        with patch.object(NeuralEdgeRepository, "get_node_degree", new=spy):
            hints = await service._generate_explore_hints(
                [result(m3), result(m2), result(m1)],
                user_id=g["a"],
                context_id=g["ctx"],
                workspace_id=g["ws"],
                neural_enabled=True,
            )

        # Read for the link set, and M2's two parallel rows are one edge: M1
        # (two distinct neighbours) is the centre, not a tie with M2.
        assert seen[m2] == (g["owners"], (1, 0))
        assert seen[m1] == (g["owners"], (0, 2))
        assert [(h.memory_id, h.reason) for h in hints] == [
            (m3, "top_result"),
            (m1, "high_centrality"),
            (m2, "unexplored_neighbor"),
        ]


class TestMemoryHealthCoversTheSet:
    @pytest.mark.asyncio
    async def test_owned_contexts_include_the_linked_accounts_private_ones(self, db_session):
        a, b, c = f"a-{uuid4().hex[:6]}", f"b-{uuid4().hex[:6]}", f"c-{uuid4().hex[:6]}"
        await _link(db_session, a, b)
        _, mine = await _private_scope(db_session, a)
        ws_linked, linked = await _private_scope(db_session, b)
        _, other = await _private_scope(db_session, c)
        # A link grants ownership of PRIVATE contexts, not membership: B's
        # shared context stays out of A's report.
        ws_shared, shared = await _private_scope(db_session, b)
        (await db_session.get(Context, shared)).is_private = False
        await db_session.flush()
        await _join_as_admin(db_session, ws_linked, a)
        await _join_as_admin(db_session, ws_shared, a)

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
        await _join_as_admin(db_session, ws, a)
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

        def node(user_id: str, workspace_id, ctx) -> Memory:
            return Memory(
                id=uuid4(),
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=ctx,
                summary="live",
                content="c",
                type="note",
                client="pytest",
                embedding_status="success",
            )

        def recall_call(user_id: str, workspace_id, ctx) -> UsageStats:
            return UsageStats(
                user_id=user_id,
                endpoint="mcp:recall",
                method="POST",
                status_code=200,
                created_at=utcnow(),
                date=utcnow().date(),
                workspace_id=workspace_id,
                context_id=ctx,
            )

        def attributed_read(user_id: str, ctx) -> ContextReadAttribution:
            return ContextReadAttribution(
                user_id=user_id, context_id=ctx, endpoint="mcp:recall", created_at=utcnow()
            )

        # B's rows in B's private context, B's rows in B's shared context, and
        # an unlinked member's rows in A's shared context — for every signal.
        scopes = [(b, ws, linked), (b, ws, elsewhere), (m, ws_a, mine_shared)]
        pairs = []
        for account, workspace_id, ctx in scopes:
            src, dst = node(account, workspace_id, ctx), node(account, workspace_id, ctx)
            pairs.append((account, workspace_id, ctx, src.id, dst.id))
            db_session.add_all(
                [
                    loser(account, workspace_id, ctx),
                    src,
                    dst,
                    recall_call(account, workspace_id, ctx),
                    attributed_read(account, ctx),
                ]
            )
        await db_session.flush()
        for account, workspace_id, ctx, src_id, dst_id in pairs:
            await _graph(db_session, account, workspace_id, ctx).add_edge(src_id, dst_id)
        svc = MemoryHealthService(db_session)
        owned_ids = frozenset(cid for cid, _ in await svc._fetch_owned_contexts(a))
        assert {linked, mine_shared} <= owned_ids

        signals = await svc._fetch_signals(a, owned_ids=owned_ids)
        backlogs = signals["backlogs"]

        assert backlogs[linked]["count"] == 1
        assert elsewhere not in backlogs
        assert mine_shared not in backlogs  # M is a member, not a linked owner

        # #1867: the graph and usage signals go through the same ``_rows_of``.
        graphs = signals["graphs"]
        assert graphs[linked]["total_edges"] == 1
        assert graphs[linked]["active_memories"] == 2
        assert graphs[linked]["edges_per_memory"] == pytest.approx(0.5)
        assert elsewhere not in graphs
        assert mine_shared not in graphs

        usage = signals["usage"]
        # One billed recall (usage_stats) + one attributed cross-context read.
        assert usage[linked]["recall"] == 2
        assert usage[linked]["successful_reads"] == 2
        assert elsewhere not in usage
        assert mine_shared not in usage
