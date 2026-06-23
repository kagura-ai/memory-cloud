"""Comprehensive line+branch coverage for ``services.graph_service.GraphService``.

The module is a thin async facade over ``NeuralEdgeRepository`` (SQL-backed
graph) plus a little local arithmetic (``stats`` density, ``get_node_metrics``
centrality). Most methods are exercised against the **real** ``db_session``
(throwaway DB from ``tests/conftest.py``) with real ``Memory`` + edge rows so
the SQL paths and the repository's edge-context invariant
(``edge.workspace == src.workspace == dst.workspace``) are genuinely driven.

A handful of pure-validator / kwarg-forwarding branches (``add_node`` type
validation, ``add_edge`` origin forwarding, ``__repr__``) are covered with a
``GraphService.__new__`` instance + a mocked ``edge_repo`` — matching the house
style of the sibling ``test_graph_service_edge_types.py`` — because those
branches do not touch the DB and a mock keeps them deterministic and fast.

Branches deliberately targeted:
    - ``add_node``: valid type (no-op) vs invalid type (ValueError).
    - ``has_node``: degree>0 (True) vs degree==0 (False).
    - ``add_edge``: rel_type validator (accept/reject), origin validator
      (None default / explicit valid / invalid reject), origin-forwarding
      branch (omitted vs explicit), str-vs-UUID id coercion.
    - ``get_edge`` / ``has_edge``: found (dict shape) vs missing (None/False).
    - ``remove_edge``: deletes existing edge.
    - ``remove_node``: deletes all incident edges.
    - ``get_neighbors``: direct neighbors via BFS, str-id coercion.
    - ``stats``: empty graph (density 0.0), populated graph (density>0,
      rounding), ``owner_filter`` sentinel default vs explicit ``None`` vs
      explicit creator string.
    - ``clear``: wipes all edges for the user.
    - ``get_node_metrics``: isolated node (edge_count==0 early return),
      connected non-hub node (centrality/avg/max), hub node (>=5 edges).
    - ``sync_node_from_memory``: node-with-edges + memory present (True),
      node-without-edges (False via has_node), memory wrong user (False).
    - ``__repr__``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from models.auth import Context, Workspace
from models.memory import (
    EDGE_ORIGIN_DECLARED,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    Memory,
)
from services.graph_service import GraphService
from utils.datetime import utcnow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_scope(db, owner: str) -> tuple[UUID, UUID]:
    """Create a real Workspace + Context and return (workspace_id, context_id).

    ``memories.workspace_id`` / ``memories.context_id`` carry FK constraints
    onto ``workspaces`` / ``contexts``, so edge tests need real parent rows
    before any ``Memory`` can be inserted.
    """
    ws = Workspace(
        id=uuid4(),
        name=f"gs-ws-{uuid4().hex[:8]}",
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
        name=f"gs-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    return ws.id, ctx.id


async def _make_memory(
    db,
    user_id: str,
    workspace_id: UUID,
    context_id: UUID,
) -> UUID:
    """Insert a minimal real ``Memory`` row and return its id.

    The repository's ``_validate_edge_context_invariant`` requires both edge
    endpoints to exist as non-soft-deleted ``Memory`` rows in the *same*
    (workspace, context) as the edge being written, so every edge test needs
    real memory rows backing its node UUIDs.
    """
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


def _make_graph(
    db,
    *,
    user_id: str,
    workspace_id: UUID,
    context_id: UUID,
) -> GraphService:
    """Real ``GraphService`` bound to the live ``db_session``.

    ``GraphService.__init__`` stores ``workspace_id``/``context_id`` as the raw
    strings it forwards to the repository, so we pass ``str(uuid)``.
    """
    return GraphService(
        user_id=user_id,
        db=db,
        workspace_id=str(workspace_id),
        context_id=str(context_id),
    )


def _mock_graph(user_id: str = "mock-user") -> GraphService:
    """Bare ``GraphService`` with a mocked ``edge_repo`` for DB-free branches."""
    graph = GraphService.__new__(GraphService)
    graph.user_id = user_id
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = MagicMock()
    graph.edge_repo.create_or_update_edge = AsyncMock()
    return graph


# ---------------------------------------------------------------------------
# Node operations
# ---------------------------------------------------------------------------


class TestAddNode:
    """``add_node`` is a SQL-backend no-op that only validates ``node_type``."""

    async def test_valid_node_type_is_noop(self) -> None:
        """A valid node_type returns None *and* writes nothing to the repo."""
        graph = _mock_graph()
        result = await graph.add_node(uuid4(), node_type="memory")
        assert result is None
        # "no-op" must mean exactly that: no edge write was issued.
        graph.edge_repo.create_or_update_edge.assert_not_called()

    @pytest.mark.parametrize("node_type", ["user", "topic"])
    async def test_all_allowed_node_types_accepted(self, node_type: str) -> None:
        """Every allowed node_type is accepted (no ValueError) and writes nothing."""
        graph = _mock_graph()
        assert await graph.add_node(uuid4(), node_type=node_type) is None
        graph.edge_repo.create_or_update_edge.assert_not_called()

    async def test_invalid_node_type_raises(self) -> None:
        """An unknown node_type fails fast with a descriptive ValueError."""
        graph = _mock_graph()
        with pytest.raises(ValueError, match="Invalid node_type"):
            await graph.add_node(uuid4(), node_type="not_a_type")


class TestHasNode:
    """``has_node`` is True iff the node has any incident edge (degree>0)."""

    async def test_true_when_node_has_edges(self, db_session) -> None:
        """A node with at least one edge reports as present."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(src, dst, weight=1.0)
        await db_session.flush()

        assert await graph.has_node(src) is True
        assert await graph.has_node(dst) is True

    async def test_false_when_node_has_no_edges(self, db_session) -> None:
        """An isolated / unknown node reports as absent (degree 0)."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        # str-id coercion path: pass a string UUID.
        assert await graph.has_node(str(uuid4())) is False


class TestRemoveNode:
    """``remove_node`` deletes every edge incident to the node."""

    async def test_removes_all_incident_edges(self, db_session) -> None:
        """After removal the node has no neighbors and no edges remain."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        a = await _make_memory(db_session, user, ws, ctx)
        b = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(center, a, weight=1.0)
        await graph.add_edge(b, center, weight=1.0)
        await db_session.flush()
        assert await graph.has_node(center) is True

        await graph.remove_node(str(center))
        await db_session.flush()

        assert await graph.has_node(center) is False
        # The two peers each lost their only edge → also gone.
        assert await graph.has_node(a) is False
        assert await graph.has_node(b) is False


# ---------------------------------------------------------------------------
# Edge operations
# ---------------------------------------------------------------------------


class TestAddEdge:
    """``add_edge`` validates rel_type + origin and forwards to the repo."""

    async def test_creates_real_edge(self, db_session) -> None:
        """A valid edge round-trips: ``get_edge`` returns its data."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)

        await graph.add_edge(src, dst, rel_type=EDGE_TYPE_DEPENDS_ON, weight=0.7)
        await db_session.flush()

        edge = await graph.get_edge(src, dst)
        assert edge is not None
        assert edge["type"] == EDGE_TYPE_DEPENDS_ON
        assert edge["weight"] == pytest.approx(0.7)

    async def test_rejects_invalid_rel_type(self) -> None:
        """An unknown rel_type fails before reaching the repository."""
        graph = _mock_graph()
        with pytest.raises(ValueError, match="Invalid rel_type"):
            await graph.add_edge(uuid4(), uuid4(), rel_type="bogus")
        graph.edge_repo.create_or_update_edge.assert_not_awaited()

    async def test_default_origin_not_forwarded(self) -> None:
        """When ``origin`` is omitted it is NOT forwarded (repo default wins)."""
        graph = _mock_graph()
        await graph.add_edge(uuid4(), uuid4(), rel_type=EDGE_TYPE_NEURAL_ASSOCIATION)
        kwargs = graph.edge_repo.create_or_update_edge.await_args.kwargs
        assert "origin" not in kwargs
        # Always pins these invariant kwargs regardless of caller.
        assert kwargs["protect_declared_link"] is True
        assert kwargs["return_fresh_edge"] is False

    async def test_explicit_valid_origin_forwarded(self) -> None:
        """An explicit, valid ``origin`` is forwarded verbatim to the repo."""
        graph = _mock_graph()
        await graph.add_edge(
            uuid4(),
            uuid4(),
            rel_type=EDGE_TYPE_NEURAL_ASSOCIATION,
            origin=EDGE_ORIGIN_DECLARED,
        )
        kwargs = graph.edge_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["origin"] == EDGE_ORIGIN_DECLARED

    async def test_rejects_invalid_origin(self) -> None:
        """A non-None unrecognized ``origin`` fails fast at the service boundary."""
        graph = _mock_graph()
        with pytest.raises(ValueError, match="Invalid origin"):
            await graph.add_edge(
                uuid4(),
                uuid4(),
                rel_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                origin="not_an_origin",
            )
        graph.edge_repo.create_or_update_edge.assert_not_awaited()

    async def test_str_ids_are_coerced_to_uuid(self) -> None:
        """String ids are coerced to ``UUID`` before reaching the repo."""
        graph = _mock_graph()
        src, dst = uuid4(), uuid4()
        await graph.add_edge(str(src), str(dst), rel_type=EDGE_TYPE_NEURAL_ASSOCIATION)
        kwargs = graph.edge_repo.create_or_update_edge.await_args.kwargs
        assert kwargs["src_id"] == src
        assert isinstance(kwargs["src_id"], UUID)
        assert kwargs["dst_id"] == dst


class TestGetEdgeAndHasEdge:
    """``get_edge`` returns a serialized dict or None; ``has_edge`` a bool."""

    async def test_get_edge_returns_full_dict(self, db_session) -> None:
        """A present edge serializes every documented field."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(src, dst, weight=1.5, confidence=0.9)
        await db_session.flush()

        edge = await graph.get_edge(str(src), str(dst))
        assert edge is not None
        assert edge["src"] == str(src)
        assert edge["dst"] == str(dst)
        assert edge["weight"] == pytest.approx(1.5)
        assert edge["confidence"] == pytest.approx(0.9)
        assert set(edge) == {
            "src",
            "dst",
            "type",
            "weight",
            "confidence",
            "metadata",
            "created_at",
            "last_updated",
        }
        # ``to_utc_iso`` produces a Z-suffixed ISO string for the timestamps.
        assert edge["created_at"].endswith("Z")

    async def test_get_edge_missing_returns_none(self, db_session) -> None:
        """A non-existent edge yields ``None`` (the ``if not edge`` branch)."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        assert await graph.get_edge(uuid4(), uuid4()) is None

    async def test_has_edge_true_and_false(self, db_session) -> None:
        """``has_edge`` is True for a present edge, False otherwise."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(src, dst, weight=1.0)
        await db_session.flush()

        assert await graph.has_edge(src, dst) is True
        # reversed direction has no edge → False
        assert await graph.has_edge(dst, src) is False


class TestRemoveEdge:
    """``remove_edge`` deletes the directed edge if present."""

    async def test_removes_existing_edge(self, db_session) -> None:
        """After ``remove_edge`` the edge no longer exists."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(src, dst, weight=1.0)
        await db_session.flush()
        assert await graph.has_edge(src, dst) is True

        await graph.remove_edge(src, dst)
        await db_session.flush()
        assert await graph.has_edge(src, dst) is False

    async def test_remove_missing_edge_is_silent(self, db_session) -> None:
        """Removing a non-existent edge is a no-op (skips the log branch)."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        # Should not raise even though nothing matches.
        await graph.remove_edge(str(uuid4()), str(uuid4()))


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------


class TestGetNeighbors:
    """``get_neighbors`` runs the repo BFS and stringifies node ids."""

    async def test_direct_neighbors(self, db_session) -> None:
        """The default ``max_hops=1`` returns the one-edge-away neighbors.

        Regression test for the BFS off-by-one fix: ``max_hops=1`` is
        documented as "direct neighbors" and must surface every node one
        edge away (and nothing further).
        """
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        n1 = await _make_memory(db_session, user, ws, ctx)
        n2 = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(center, n1, weight=1.0)
        await graph.add_edge(center, n2, weight=1.0)
        await db_session.flush()

        # Default max_hops=1 → exactly the two direct neighbors.
        neighbors = await graph.get_neighbors(str(center))
        assert isinstance(neighbors, list)
        assert all(isinstance(x, str) for x in neighbors)
        assert set(neighbors) == {str(n1), str(n2)}

    async def test_max_hops_one_excludes_two_hop_nodes(self, db_session) -> None:
        """``max_hops=1`` returns the direct neighbor but NOT a 2-hop node.

        Regression test for the BFS off-by-one: the depth guard must bound
        *expansion*, not *recording*. center→n1→n2 — with max_hops=1 we see
        n1 (direct) and never n2 (two edges away).
        """
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        n1 = await _make_memory(db_session, user, ws, ctx)
        n2 = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(center, n1, weight=1.0)
        await graph.add_edge(n1, n2, weight=1.0)
        await db_session.flush()

        # max_hops=1 → only the direct neighbor n1.
        assert await graph.get_neighbors(str(center), max_hops=1) == [str(n1)]

    async def test_max_hops_two_includes_two_hop_nodes(self, db_session) -> None:
        """``max_hops=2`` reaches both the direct and the two-hop neighbor."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        n1 = await _make_memory(db_session, user, ws, ctx)
        n2 = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(center, n1, weight=1.0)
        await graph.add_edge(n1, n2, weight=1.0)
        await db_session.flush()

        neighbors = await graph.get_neighbors(str(center), max_hops=2)
        assert set(neighbors) == {str(n1), str(n2)}

    async def test_no_neighbors_returns_empty(self, db_session) -> None:
        """A node with no outgoing edges yields an empty neighbor list."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        lonely = await _make_memory(db_session, user, ws, ctx)
        assert await graph.get_neighbors(lonely) == []


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class TestStats:
    """``stats`` aggregates edge counts/weights and derives node density."""

    async def test_empty_graph_density_zero(self, db_session) -> None:
        """An empty graph reports zero counts and density 0.0 (no div-by-zero)."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        stats = await graph.stats()
        assert stats["total_nodes"] == 0
        assert stats["total_edges"] == 0
        assert stats["density"] == 0.0
        assert stats["avg_edge_weight"] == 0.0

    async def test_populated_graph_density_and_rounding(self, db_session) -> None:
        """A populated graph reports node/edge counts and a positive density.

        Two distinct nodes with one directed edge: ``max_possible_edges`` =
        2*1 = 2, so density = 1/2 = 0.5 — exercising the
        ``max_possible_edges > 0`` true branch and the ``round(..., 4)`` paths.
        """
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        a = await _make_memory(db_session, user, ws, ctx)
        b = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(a, b, weight=2.0, confidence=1.0)
        await db_session.flush()

        stats = await graph.stats()
        assert stats["total_nodes"] == 2
        assert stats["total_edges"] == 1
        assert stats["density"] == pytest.approx(0.5)
        assert stats["avg_edge_weight"] == pytest.approx(2.0)
        assert stats["max_edge_weight"] == pytest.approx(2.0)
        assert stats["min_edge_weight"] == pytest.approx(2.0)

    async def test_owner_filter_explicit_none_aggregates(self, db_session) -> None:
        """Explicit ``owner_filter=None`` drops the creator filter.

        The edge is authored by ``user`` but the GraphService is constructed
        with a *different* ``self.user_id``. With the sentinel default the
        creator filter would hide the edge; passing ``None`` aggregates across
        creators in the workspace+context, so the edge is counted.
        """
        author = f"author-{uuid4()}"
        other = f"viewer-{uuid4()}"
        ws, ctx = await _make_scope(db_session, author)
        author_graph = _make_graph(db_session, user_id=author, workspace_id=ws, context_id=ctx)
        a = await _make_memory(db_session, author, ws, ctx)
        b = await _make_memory(db_session, author, ws, ctx)
        await author_graph.add_edge(a, b, weight=1.0)
        await db_session.flush()

        viewer_graph = _make_graph(db_session, user_id=other, workspace_id=ws, context_id=ctx)
        # Sentinel default → filters by viewer's own (different) user_id → 0.
        scoped = await viewer_graph.stats()
        assert scoped["total_edges"] == 0
        # Explicit None → no creator filter → sees the author's edge.
        shared = await viewer_graph.stats(owner_filter=None)
        assert shared["total_edges"] == 1

    async def test_owner_filter_explicit_creator(self, db_session) -> None:
        """An explicit creator string restricts to that creator's edges."""
        author = f"author-{uuid4()}"
        ws, ctx = await _make_scope(db_session, author)
        graph = _make_graph(db_session, user_id="someone-else", workspace_id=ws, context_id=ctx)
        a = await _make_memory(db_session, author, ws, ctx)
        b = await _make_memory(db_session, author, ws, ctx)
        # author writes the edge directly via an author-scoped service
        author_graph = _make_graph(db_session, user_id=author, workspace_id=ws, context_id=ctx)
        await author_graph.add_edge(a, b, weight=1.0)
        await db_session.flush()

        # Restrict to the author explicitly → counted.
        assert (await graph.stats(owner_filter=author))["total_edges"] == 1
        # Restrict to a creator with no edges → zero.
        assert (await graph.stats(owner_filter=f"nobody-{uuid4()}"))["total_edges"] == 0


class TestClear:
    """``clear`` removes every edge owned by the user."""

    async def test_clear_removes_user_edges(self, db_session) -> None:
        """After ``clear`` the user's graph is empty."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        a = await _make_memory(db_session, user, ws, ctx)
        b = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(a, b, weight=1.0)
        await db_session.flush()
        assert (await graph.stats())["total_edges"] == 1

        await graph.clear()
        await db_session.flush()
        assert (await graph.stats())["total_edges"] == 0


# ---------------------------------------------------------------------------
# Node metrics
# ---------------------------------------------------------------------------


class TestGetNodeMetrics:
    """``get_node_metrics`` derives centrality/hub/isolation per node."""

    async def test_isolated_node_early_return(self, db_session) -> None:
        """A node with zero edges returns the isolated-node metric block."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        node = await _make_memory(db_session, user, ws, ctx)

        metrics = await graph.get_node_metrics(str(node))
        assert metrics == {
            "centrality": 0.0,
            "edge_count": 0,
            "avg_edge_weight": 0.0,
            "max_edge_weight": 0.0,
            "is_hub_node": False,
            "is_isolated": True,
        }

    async def test_connected_non_hub_node(self, db_session) -> None:
        """A node with <5 edges reports non-hub metrics with real weights."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        peers = [await _make_memory(db_session, user, ws, ctx) for _ in range(2)]
        # one outgoing, one incoming → edge_count 2
        await graph.add_edge(center, peers[0], weight=1.0)
        await graph.add_edge(peers[1], center, weight=3.0)
        await db_session.flush()

        metrics = await graph.get_node_metrics(center)
        assert metrics["edge_count"] == 2
        assert metrics["is_hub_node"] is False
        assert metrics["is_isolated"] is False
        assert metrics["avg_edge_weight"] == pytest.approx(2.0)
        assert metrics["max_edge_weight"] == pytest.approx(3.0)
        # centrality = edge_count / (total_nodes - 1). total_nodes == 3 here.
        assert metrics["centrality"] == pytest.approx(2 / 2)

    async def test_hub_node(self, db_session) -> None:
        """A node with >=5 edges is flagged as a hub."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        center = await _make_memory(db_session, user, ws, ctx)
        for _ in range(5):
            peer = await _make_memory(db_session, user, ws, ctx)
            await graph.add_edge(center, peer, weight=1.0)
        await db_session.flush()

        metrics = await graph.get_node_metrics(center)
        assert metrics["edge_count"] == 5
        assert metrics["is_hub_node"] is True
        assert metrics["is_isolated"] is False


# ---------------------------------------------------------------------------
# Sync + repr
# ---------------------------------------------------------------------------


class TestSyncNodeFromMemory:
    """``sync_node_from_memory`` is a no-op that returns True/False."""

    async def test_true_when_node_and_memory_present(self, db_session) -> None:
        """A node with edges whose memory belongs to the user returns True."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        src = await _make_memory(db_session, user, ws, ctx)
        dst = await _make_memory(db_session, user, ws, ctx)
        await graph.add_edge(src, dst, weight=1.0)
        await db_session.flush()

        assert await graph.sync_node_from_memory(str(src)) is True

    async def test_false_when_node_has_no_edges(self, db_session) -> None:
        """A node with no edges short-circuits via ``has_node`` → False."""
        user = f"user-{uuid4()}"
        ws, ctx = await _make_scope(db_session, user)
        graph = _make_graph(db_session, user_id=user, workspace_id=ws, context_id=ctx)
        # Unknown id → has_node False → returns False before the Memory query.
        assert await graph.sync_node_from_memory(uuid4()) is False

    async def test_false_when_memory_belongs_to_other_user(self, db_session) -> None:
        """A node whose backing memory has a different user_id returns False.

        ``has_node`` is creator-scoped (it counts edges where
        ``edge.user_id == self.user_id``), so to reach the
        ``memory.user_id != self.user_id`` warning branch the *edge* must be
        owned by ``self.user_id`` while the backing *Memory* row is owned by
        someone else. The edge-context invariant only ties the edge to the
        memories' (workspace, context) — not their ``user_id`` — so a viewer
        can legitimately hold an edge over another creator's memory. That row
        drives ``has_node`` True + Memory.user_id mismatch → sync returns False.
        """
        owner = f"owner-{uuid4()}"
        viewer = f"viewer-{uuid4()}"
        ws, ctx = await _make_scope(db_session, owner)
        # Memories owned by `owner`...
        src = await _make_memory(db_session, owner, ws, ctx)
        dst = await _make_memory(db_session, owner, ws, ctx)
        # ...but the edge is authored by `viewer`.
        viewer_graph = _make_graph(db_session, user_id=viewer, workspace_id=ws, context_id=ctx)
        await viewer_graph.add_edge(src, dst, weight=1.0)
        await db_session.flush()

        # viewer's has_node(src) is True (viewer owns the edge), but
        # Memory(src).user_id == owner != viewer → mismatch branch → False.
        assert await viewer_graph.has_node(src) is True
        assert await viewer_graph.sync_node_from_memory(src) is False


class TestRepr:
    """``__repr__`` summarizes the service without an async call."""

    def test_repr_includes_user_and_backend(self) -> None:
        """The repr names the user and the SQL backend."""
        graph = _mock_graph(user_id="repr-user")
        text = repr(graph)
        assert "repr-user" in text
        assert "SQL" in text


class TestConstructorWiring:
    """``__init__`` wires the repo and stores isolation ids."""

    async def test_init_stores_fields(self, db_session) -> None:
        """The constructor stores user/workspace/context and builds the repo."""
        ws, ctx = uuid4(), uuid4()
        graph = GraphService(
            user_id="ctor-user",
            db=db_session,
            workspace_id=str(ws),
            context_id=str(ctx),
        )
        assert graph.user_id == "ctor-user"
        assert graph.workspace_id == str(ws)
        assert graph.context_id == str(ctx)
        assert graph.edge_repo is not None
        # the repo is wired with the same DB session the service was given
        assert graph.edge_repo.db is db_session
