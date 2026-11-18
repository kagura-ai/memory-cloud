"""Every declared ``supersedes`` edge writer shares the feed ownership fence (#2907).

Real PostgreSQL sessions: the fence guards are no-ops for mocked sessions (see
``tests/conftest.py``), so only a database proves which writers are fenced. The
writers walked are MCP ``create_edge`` / ``update_edge`` / ``delete_edge`` and
REST ``POST /api/v1/graph/edges``. Creating, retyping to or from, re-asserting,
reweighting or deleting a ``supersedes`` edge on a ``feed`` / ``stopping``
target (or a retained one not enrolled yet) is 409 ``SYNC-LOCKED``; nothing is
written and the stored supersede suggestion stays. ``related_to`` /
``contradicts`` edges stay writable, and ``manual`` / unmanaged contexts behave
as before.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from config.settings import get_settings
from mcp_server.tools._errors import _tool_exception_response
from mcp_server.tools.edge import handle_create_edge, handle_delete_edge, handle_update_edge
from models.memory import NeuralMemoryEdge
from models.public_mcp import PublicMcpEndpoint
from models.sync_installation import SyncInstallation, SyncService
from models.sync_management import SyncManagedTarget
from services.edge_service import create_declared_edge
from services.sync_managed_guard import target_fence
from tests.observability_helpers import owner_client, world_rows
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")

EDGES = "/api/v1/graph/edges"
FENCED_MODES = ["feed", "stopping", "retained"]
OPEN_MODES = ["unmanaged", "manual"]
HANDLERS = {
    "create_edge": handle_create_edge,
    "update_edge": handle_update_edge,
    "delete_edge": handle_delete_edge,
}


@pytest_asyncio.fixture(loop_scope="session")
async def graph(async_engine, monkeypatch):
    """World A's context a1 with four of the owner's memories and two declared edges.

    ``a → b`` is ``supersedes``, ``a → c`` is ``related_to``, and ``a`` carries a
    stored suggestion naming ``d``. Context a2 holds ``x → y`` (``supersedes``).
    """
    from models.memory import Memory

    monkeypatch.setattr(get_settings(), "sync_managed_enabled", True)
    monkeypatch.setattr("mcp_server.tools.edge._log_tool_usage", AsyncMock())
    async with world_rows(async_engine) as w:
        ids = {name: uuid4() for name in ("a", "b", "c", "d", "x", "y")}
        async with w.factory() as s:
            for name, memory_id in ids.items():
                s.add(
                    Memory(
                        id=memory_id,
                        user_id=w.owner,
                        workspace_id=w.ws,
                        context_id=w.ctx["a2" if name in ("x", "y") else "a1"],
                        summary=f"Synthetic memory {name}",
                        content=f"Synthetic content {name}",
                        type="note",
                        client="pytest",
                        embedding_status="success",
                        supersede_candidate=(
                            {"memory_id": str(ids["d"]), "summary": "d", "similarity": 0.95}
                            if name == "a"
                            else None
                        ),
                    )
                )
            await s.flush()
            for src, dst, edge_type, ctx in (
                ("a", "b", "supersedes", "a1"),
                ("a", "c", "related_to", "a1"),
                ("x", "y", "supersedes", "a2"),
            ):
                s.add(
                    NeuralMemoryEdge(
                        user_id=w.owner,
                        src_id=ids[src],
                        dst_id=ids[dst],
                        edge_type=edge_type,
                        weight=1.0,
                        confidence=1.0,
                        origin="declared",
                        workspace_id=w.ws,
                        context_id=w.ctx[ctx],
                    )
                )
            endpoint = PublicMcpEndpoint(
                id=uuid4(),
                workspace_id=w.ws,
                resource_pk=w.resource_pk["a1"],
                context_id=w.ctx["a1"],
                resource_id=w.slugs["a1"],
                public_name="Synthetic public resource",
                created_by=w.owner,
            )
            s.add(endpoint)
            await s.commit()
        w.ids = ids  # type: ignore[attr-defined]
        w.endpoint_id = endpoint.id  # type: ignore[attr-defined]
        yield w


@contextlib.asynccontextmanager
async def managed_as(w, mode: str) -> AsyncIterator[None]:
    """Make context a1 feed-owned in ``mode``; ``retained`` = installation not enrolled yet."""
    if mode == "unmanaged":
        yield
        return
    service_id = "edge-fence-" + uuid4().hex[:12]
    async with w.factory() as s:
        s.add(
            SyncService(
                service_id=service_id,
                browser_client_id=service_id + "-browser",
                service_client_id=service_id + "-service",
            )
        )
        await s.flush()
        installation = SyncInstallation(
            workspace_id=w.ws,
            service_id=service_id,
            service_client_id=service_id + "-service",
            authorization_id=uuid4(),
            application_id="edge-fence",
            operations=[],
            resource_pk=w.resource_pk["a1"],
            context_id=w.ctx["a1"],
            endpoint_id=w.endpoint_id,
            resource_id=w.slugs["a1"],
        )
        s.add(installation)
        await s.flush()
        if mode != "retained":
            s.add(
                SyncManagedTarget(
                    installation_id=installation.id,
                    resource_pk=w.resource_pk["a1"],
                    context_id=w.ctx["a1"],
                    mode=mode,
                    generation=2 if mode == "stopping" else 1,
                    drain_generation=1 if mode == "stopping" else None,
                )
            )
        await s.commit()
    try:
        yield
    finally:
        async with w.factory() as s:
            await s.execute(delete(SyncInstallation).where(SyncInstallation.workspace_id == w.ws))
            await s.execute(delete(SyncService).where(SyncService.service_id == service_id))
            await s.commit()


async def mcp(w, tool: str, ctx: str = "a1", **args: Any) -> dict:
    """Run an edge tool as the dispatcher does: a raised refusal becomes its envelope."""

    async def _get_db():
        async with w.factory() as s:
            yield s

    for key in ("source_id", "target_id"):
        if key in args:
            args[key] = str(w.ids[args[key]])
    with patch("db.base.get_db", new=_get_db):
        try:
            result = await HANDLERS[tool]({"context_id": str(w.ctx[ctx]), **args}, w.owner, w.ws)
        except Exception as exc:  # noqa: BLE001 - the dispatcher's own conversion
            result = _tool_exception_response(tool, exc)
    return json.loads(result[0].text)


@contextlib.asynccontextmanager
async def session_client(w) -> AsyncIterator[httpx.AsyncClient]:
    """The real app with the owner as the browser-session caller."""
    from api.main import app
    from auth.dependencies import require_session_auth

    async def _user() -> dict[str, Any]:
        return {
            "user_id": w.owner,
            "email": f"{w.owner}@example.test",
            "role": "user",
            "current_workspace_id": w.ws,
        }

    app.dependency_overrides[require_session_auth] = _user
    try:
        async with owner_client(w, None) as client:
            yield client
    finally:
        app.dependency_overrides.pop(require_session_auth, None)


def rest_body(w, src: str, dst: str, edge_type: str, **extra: Any) -> dict:
    return {
        "context_id": str(w.ctx["a1"]),
        "source_id": str(w.ids[src]),
        "target_id": str(w.ids[dst]),
        "edge_type": edge_type,
        **extra,
    }


async def edges(w) -> dict[tuple[UUID, UUID], tuple]:
    async with w.factory() as s:
        rows = (
            await s.scalars(select(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id == w.ws))
        ).all()
    return {
        (e.src_id, e.dst_id): (e.edge_type, e.weight, e.confidence, e.origin, e.context_id)
        for e in rows
    }


async def suggestion(w) -> dict | None:
    from models.memory import Memory

    async with w.factory() as s:
        return await s.scalar(select(Memory.supersede_candidate).where(Memory.id == w.ids["a"]))


def assert_locked(reply: dict, mode: str) -> None:
    assert reply["status"] == "error", reply
    assert reply["error"] == "conflict", reply
    assert reply["management_mode"] == ("feed" if mode == "retained" else mode), reply
    assert "feed-managed" in reply["message"]


@pytest.mark.parametrize("mode", FENCED_MODES)
async def test_every_supersedes_edge_writer_is_refused_on_a_feed_managed_context(graph, mode):
    w = graph
    before_edges, before_suggestion = await edges(w), await suggestion(w)
    async with managed_as(w, mode):
        replies = [
            # create, a re-assert of the existing one, and a retype away from it
            await mcp(w, "create_edge", source_id="a", target_id="d", edge_type="supersedes"),
            await mcp(w, "create_edge", source_id="a", target_id="b", edge_type="supersedes"),
            await mcp(
                w,
                "create_edge",
                source_id="a",
                target_id="b",
                edge_type="related_to",
                overwrite=True,
            ),
            # reweight, retype away from, retype to
            await mcp(w, "update_edge", source_id="a", target_id="b", weight=0.5),
            await mcp(w, "update_edge", source_id="a", target_id="b", edge_type="related_to"),
            await mcp(w, "update_edge", source_id="a", target_id="c", edge_type="supersedes"),
            await mcp(w, "delete_edge", source_id="a", target_id="b"),
        ]
        async with session_client(w) as c:
            rest = [
                await c.post(EDGES, json=rest_body(w, "a", "d", "supersedes")),
                await c.post(EDGES, json=rest_body(w, "a", "b", "related_to", overwrite=True)),
            ]
    for reply in replies:
        assert_locked(reply, mode)
    for response in rest:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "SYNC-LOCKED"
        assert response.json()["details"]["management_mode"] == (
            "feed" if mode == "retained" else mode
        )
    assert await edges(w) == before_edges
    assert await suggestion(w) == before_suggestion


@pytest.mark.parametrize("mode", FENCED_MODES)
async def test_related_to_and_contradicts_edges_stay_writable_on_a_feed_managed_context(
    graph, mode
):
    w = graph
    async with managed_as(w, mode):
        created = await mcp(w, "create_edge", source_id="b", target_id="c", edge_type="contradicts")
        updated = await mcp(w, "update_edge", source_id="a", target_id="c", weight=0.4)
        retyped = await mcp(w, "update_edge", source_id="b", target_id="c", edge_type="related_to")
        async with session_client(w) as c:
            rest = await c.post(EDGES, json=rest_body(w, "c", "d", "related_to"))
        after = await edges(w)
        deleted = await mcp(w, "delete_edge", source_id="b", target_id="c")
    assert created["status"] == "success" and created["operation"] == "created", created
    assert updated["status"] == "success", updated
    assert retyped["status"] == "success", retyped
    assert rest.status_code == 201, rest.text
    ids = w.ids
    assert after[(ids["a"], ids["c"])][:2] == ("related_to", 0.4)
    assert after[(ids["b"], ids["c"])][0] == "related_to"
    assert after[(ids["c"], ids["d"])][0] == "related_to"
    assert deleted["status"] == "success", deleted
    assert (ids["b"], ids["c"]) not in await edges(w)
    assert (ids["a"], ids["b"]) in await edges(w)


@pytest.mark.parametrize("mode", OPEN_MODES)
async def test_supersedes_edge_writers_keep_working_on_manual_and_unmanaged_contexts(graph, mode):
    w = graph
    ids = w.ids
    async with managed_as(w, mode):
        created = await mcp(w, "create_edge", source_id="a", target_id="d", edge_type="supersedes")
        accepted = await suggestion(w)
        reweighted = await mcp(w, "update_edge", source_id="a", target_id="b", weight=0.5)
        retyped = await mcp(w, "update_edge", source_id="a", target_id="c", edge_type="supersedes")
        async with session_client(w) as c:
            rest = await c.post(EDGES, json=rest_body(w, "c", "d", "supersedes"))
        deleted = await mcp(w, "delete_edge", source_id="a", target_id="b")
    assert created["status"] == "success" and created["operation"] == "created", created
    # The accepted suggestion is cleared in the same transaction as the edge.
    assert accepted is None
    assert reweighted["status"] == "success", reweighted
    assert retyped["status"] == "success", retyped
    assert rest.status_code == 201, rest.text
    assert deleted["status"] == "success", deleted
    after = await edges(w)
    assert after[(ids["a"], ids["d"])][0] == "supersedes"
    assert after[(ids["a"], ids["c"])][0] == "supersedes"
    assert after[(ids["c"], ids["d"])][0] == "supersedes"
    assert (ids["a"], ids["b"]) not in after


async def test_an_edge_whose_endpoint_moved_into_a_feed_context_is_fenced_there(graph):
    """The edge's own context and each endpoint's current context are all fenced."""
    from models.memory import Memory

    w = graph
    async with w.factory() as s:
        await s.execute(
            update(Memory).where(Memory.id == w.ids["y"]).values(context_id=w.ctx["a1"])
        )
        await s.commit()
    before = await edges(w)
    async with managed_as(w, "feed"):
        deleted = await mcp(w, "delete_edge", ctx="a2", source_id="x", target_id="y")
        retyped = await mcp(
            w,
            "create_edge",
            ctx="a2",
            source_id="x",
            target_id="y",
            edge_type="related_to",
            overwrite=True,
        )
    assert_locked(deleted, "feed")
    assert_locked(retyped, "feed")
    assert await edges(w) == before
    # The same edge is an ordinary one once the context it reaches is not managed.
    deleted = await mcp(w, "delete_edge", ctx="a2", source_id="x", target_id="y")
    assert deleted["status"] == "success", deleted


async def test_a_mode_transition_refuses_an_edge_write_in_flight_and_vice_versa(graph):
    """Two sessions: the mode an edge write decided on cannot change before it commits."""
    w = graph
    ctx = UUID(str(w.ctx["a1"]))
    before = await edges(w)
    async with w.factory() as transition:
        await target_fence(transition, ctx, exclusive=True)
        reply = await mcp(w, "create_edge", source_id="a", target_id="d", edge_type="supersedes")
        assert reply["error"] == "conflict", reply
        assert reply["message"] == "Feed management is changing; retry"
        await transition.rollback()
    assert await edges(w) == before
    async with managed_as(w, "manual"), w.factory() as writer:
        result = await create_declared_edge(
            writer,
            user_id=w.owner,
            source_id=w.ids["b"],
            target_id=w.ids["d"],
            edge_type="related_to",
            weight=1.0,
            confidence=1.0,
            workspace_id=str(w.ws),
            context_id=str(ctx),
            overwrite=False,
        )
        assert result.operation == "created"
        # A plain related_to write still holds the shared fence until it commits.
        async with w.factory() as transition:
            with pytest.raises(ConflictError):
                await target_fence(transition, ctx, exclusive=True)
            await transition.rollback()
        await writer.commit()
        async with w.factory() as transition:
            await target_fence(transition, ctx, exclusive=True)
            await transition.rollback()
