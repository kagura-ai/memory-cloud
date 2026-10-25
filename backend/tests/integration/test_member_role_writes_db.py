"""Memory and edge writes need the member role on the target context's workspace.

On real rows, through ``execute_tool_call`` (MCP) and the FastAPI app (REST):

* a workspace viewer cannot create, change or delete memories or edges in a
  context of that workspace, whichever workspace the MCP session points at
  (the context's own, the viewer's personal one, or none), and nothing in the
  database changes;
* that holds for a private context the viewer created, and for a memory
  addressed by id while the declared context is one the viewer owns;
* a member and a context editor still write, the member from a session in
  another workspace too, and an edge is stored in the context's workspace;
* a workspace-scoped key reaches only its own workspace in the context tools
  (update_context, delete_context, update_search_config) and the REST
  search-config routes.

Embedding, vector store and quota calls are stubbed; every session is on the
test database (``db.base`` is pointed at the test engine as well).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import ContextRole, WorkspaceRole
from mcp_server.tools._capacity_gate import invalidate_capacity_candidate_cache
from mcp_server.tools._helpers import _mcp_key_workspace_scope
from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember
from models.memory import Memory, NeuralMemoryEdge

pytestmark = pytest.mark.asyncio(loop_scope="session")

ORIGINAL_CONTENT = "Failover runbook: promote replica B"


@dataclass
class World:
    session: AsyncSession
    factory: async_sessionmaker[AsyncSession]
    owner: str
    viewer: str
    member: str
    editor: str
    w: UUID  # the shared workspace (owner)
    p: UUID  # the viewer's personal workspace
    a: UUID  # a second workspace of the owner
    mp: UUID  # the member's personal workspace
    ctx: UUID  # shared context of W
    viewer_private: UUID  # private context of W created by the viewer
    viewer_personal_ctx: UUID  # context of P
    a_ctx: UUID  # context of A
    real: UUID  # the owner's memory in ctx
    decoy: UUID  # another owner memory in ctx
    viewer_own: UUID  # the viewer's memory in ctx (written before it became a viewer)
    viewer_private_mem: UUID  # the viewer's memory in its private context


def _workspace(owner: str, tag: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"mrw-{tag}-{uuid4().hex[:6]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _memory(ws: UUID, ctx: UUID, author: str, summary: str) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content=ORIGINAL_CONTENT,
        type="decision",
        scope="persistent",
        client="test",
        importance=0.9,
        embedding_status="success",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    invalidate_capacity_candidate_cache()

    tag = uuid4().hex[:8]
    owner, viewer, member, editor = (f"mrw-{r}-{tag}" for r in ("own", "vie", "mem", "edi"))
    async with factory() as session:
        for u in (owner, viewer, member, editor):
            session.add(User(email=f"{u}@example.test", user_id=u, name=u, role="user"))
        await session.flush()
        w, a = _workspace(owner, "w"), _workspace(owner, "a")
        p, mp = _workspace(viewer, "p"), _workspace(member, "mp")
        session.add_all([w, a, p, mp])
        await session.flush()
        ctx = Context(
            id=uuid4(), workspace_id=w.id, name=f"c-{tag}", created_by=owner, is_private=False
        )
        viewer_private = Context(
            id=uuid4(), workspace_id=w.id, name=f"vp-{tag}", created_by=viewer, is_private=True
        )
        viewer_personal_ctx = Context(
            id=uuid4(), workspace_id=p.id, name=f"p-{tag}", created_by=viewer, is_private=False
        )
        a_ctx = Context(
            id=uuid4(), workspace_id=a.id, name=f"a-{tag}", created_by=owner, is_private=False
        )
        session.add_all([ctx, viewer_private, viewer_personal_ctx, a_ctx])
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(workspace_id=w.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=a.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=w.id, user_id=viewer, role=WorkspaceRole.VIEWER),
                WorkspaceMember(workspace_id=p.id, user_id=viewer, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=w.id,
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[ctx.id],
                ),
                WorkspaceMember(workspace_id=mp.id, user_id=member, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=w.id,
                    user_id=editor,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[ctx.id],
                ),
                ContextMember(context_id=ctx.id, user_id=editor, role=ContextRole.EDITOR),
            ]
        )
        real = _memory(w.id, ctx.id, owner, "Prod DB failover runbook")
        decoy = _memory(w.id, ctx.id, owner, "Old failover note, superseded")
        viewer_own = _memory(w.id, ctx.id, viewer, "A note the viewer wrote as a member")
        viewer_private_mem = _memory(w.id, viewer_private.id, viewer, "The viewer's private note")
        session.add_all([real, decoy, viewer_own, viewer_private_mem])
        await session.commit()
        # Plain values: a test's rollback expires the ORM rows.
        ws_ids = [w.id, a.id, p.id, mp.id]
        user_ids = [owner, viewer, member, editor]
        try:
            yield World(
                session=session,
                factory=factory,
                owner=owner,
                viewer=viewer,
                member=member,
                editor=editor,
                w=w.id,
                p=p.id,
                a=a.id,
                mp=mp.id,
                ctx=ctx.id,
                viewer_private=viewer_private.id,
                viewer_personal_ctx=viewer_personal_ctx.id,
                a_ctx=a_ctx.id,
                real=real.id,
                decoy=decoy.id,
                viewer_own=viewer_own.id,
                viewer_private_mem=viewer_private_mem.id,
            )
        finally:
            await session.rollback()
            for stmt in (
                delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(ws_ids)),
                delete(Workspace).where(Workspace.id.in_(ws_ids)),
                delete(User).where(User.user_id.in_(user_ids)),
            ):
                try:
                    await session.execute(stmt)
                    await session.commit()
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    await session.rollback()
            invalidate_capacity_candidate_cache()


@pytest.fixture
def live(world: World) -> Any:
    """Handlers use the world's session; embeddings, vectors and quotas are stubbed."""

    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    from services.memory_service import MemoryService

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    embedder = MagicMock(embed=AsyncMock(return_value=[0.1]))
    with (
        patch("db.base.get_db", new=_db),
        # The write paths start the embedding on a task of their own; nothing
        # may leave the test database.
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.embedding_service.EmbeddingService", return_value=embedder),
        patch("services.quota_service.QuotaService", return_value=quota),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()),
        patch("db.qdrant.delete_points_from_qdrant", new=AsyncMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch.object(MemoryService, "_delete_memory_point", new=AsyncMock()),
        patch.object(MemoryService, "_point_to_delete", new=AsyncMock(return_value=None)),
        patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))),
    ):
        yield world


async def _tool(tool: str, args: dict, user_id: str, session_ws: UUID | None, *, key=None) -> dict:
    from mcp_server.tools import execute_tool_call

    token = _mcp_key_workspace_scope.set(key)
    try:
        result = await execute_tool_call(tool, args, user_id, session_ws)
    finally:
        _mcp_key_workspace_scope.reset(token)
    return json.loads(result[0].text)


async def _row(world: World, memory_id: UUID) -> Memory:
    world.session.expunge_all()
    return (await world.session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()


async def _edges(world: World) -> list[NeuralMemoryEdge]:
    world.session.expunge_all()
    return list(
        (
            await world.session.execute(
                select(NeuralMemoryEdge).where(NeuralMemoryEdge.context_id == world.ctx)
            )
        ).scalars()
    )


async def _live_count(world: World, context_id: UUID) -> int:
    return (
        await world.session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.context_id == context_id, Memory.deleted_at.is_(None))
        )
    ).scalar_one()


def _write_calls(w: World, ctx: UUID, memory_id: UUID) -> list[tuple[str, dict]]:
    c = str(ctx)
    item = {"summary": "Viewer note that must not land", "content": "x", "type": "note"}
    return [
        ("remember", {"context_id": c, **item, "dedupe": "off"}),
        ("remember_batch", {"context_id": c, "items": [item]}),
        ("update_memory", {"context_id": c, "memory_id": str(memory_id), "content": "changed"}),
        ("update_memory", {"context_id": c, "memory_id": str(memory_id), "importance": 0.1}),
        (
            "update_memory",
            {"context_id": c, "memory_id": str(memory_id), "dismiss_supersede_candidate": True},
        ),
        ("remember", {"context_id": c, **item, "dedupe": "off", "supersedes": str(memory_id)}),
        (
            "update_memory",
            {"context_id": c, "external_id": "ext-viewer-1", **item},
        ),
        (
            "update_memory",
            {"context_id": c, "memory_id": str(memory_id), "accept_supersede_candidate": True},
        ),
        ("forget", {"context_id": c, "memory_id": str(memory_id)}),
        ("forget", {"context_id": c, "query": "failover runbook"}),
        ("forget", {"context_id": c, "memory_ids": [str(memory_id)]}),
        (
            "create_edge",
            {
                "context_id": c,
                "source_id": str(w.decoy),
                "target_id": str(w.real),
                "edge_type": "supersedes",
            },
        ),
        (
            "update_edge",
            {"context_id": c, "source_id": str(w.decoy), "target_id": str(w.real), "weight": 2.5},
        ),
        ("delete_edge", {"context_id": c, "source_id": str(w.decoy), "target_id": str(w.real)}),
        ("set_state", {"context_id": c, "key": "k", "value": "v"}),
        ("record_measurement", {"context_id": c, "metric": "weight", "value": 1.5}),
        ("rollback_session", {"context_id": c, "session_id": "s-1", "dry_run": True}),
    ]


# ---------------------------------------------------------------- MCP: viewer


@pytest.mark.parametrize("session", ["context_workspace", "personal_workspace", "no_workspace"])
async def test_a_viewer_cannot_write_in_the_workspace_from_any_session(live, session) -> None:
    w: World = live
    session_ws = {"context_workspace": w.w, "personal_workspace": w.p, "no_workspace": None}[
        session
    ]
    before = await _live_count(w, w.ctx)
    for tool, args in _write_calls(w, w.ctx, w.real):
        body = await _tool(tool, args, w.viewer, session_ws)
        assert body.get("error") == "permission_denied", (tool, args, body)

    row = await _row(w, w.real)
    assert row.deleted_at is None
    assert row.content == ORIGINAL_CONTENT and row.importance == pytest.approx(0.9)
    assert await _live_count(w, w.ctx) == before
    assert await _edges(w) == []


async def test_a_viewer_cannot_change_its_own_memory(live) -> None:
    w: World = live
    for tool, args in _write_calls(w, w.ctx, w.viewer_own):
        if tool.endswith("_edge"):
            continue
        body = await _tool(tool, args, w.viewer, w.p)
        assert body.get("error") == "permission_denied", (tool, body)
    row = await _row(w, w.viewer_own)
    assert row.deleted_at is None and row.content == ORIGINAL_CONTENT


async def test_a_viewer_is_read_only_in_a_private_context_it_created(live) -> None:
    """Consistent with rollback_session: the workspace role decides, the
    creator's owner role on its private context does not lift it."""
    w: World = live
    for tool, args in _write_calls(w, w.viewer_private, w.viewer_private_mem):
        if tool.endswith("_edge"):
            continue
        body = await _tool(tool, args, w.viewer, w.w)
        assert body.get("error") == "permission_denied", (tool, body)
    row = await _row(w, w.viewer_private_mem)
    assert row.deleted_at is None and row.content == ORIGINAL_CONTENT
    assert await _live_count(w, w.viewer_private) == 1

    # The context tools refuse it as well: the creator is a viewer here.
    renamed = await _tool(
        "update_context",
        {"context_id": str(w.viewer_private), "display_name": "renamed"},
        w.viewer,
        w.w,
    )
    assert renamed.get("error") == "permission_denied", renamed
    search = await _tool(
        "update_search_config",
        {"context_id": str(w.viewer_private), "semantic_weight": 0.5},
        w.viewer,
        w.w,
    )
    assert search.get("error") == "permission_denied", search

    # Reading it still works.
    body = await _tool(
        "reference",
        {"context_id": str(w.viewer_private), "memory_id": str(w.viewer_private_mem)},
        w.viewer,
        w.w,
    )
    assert body.get("status") == "success", body


async def test_a_memory_addressed_by_id_is_checked_on_its_own_workspace(live) -> None:
    """The declared context is the viewer's own (P); the memory is in W."""
    w: World = live
    c = str(w.viewer_personal_ctx)
    updated = await _tool(
        "update_memory",
        {"context_id": c, "memory_id": str(w.real), "content": "changed"},
        w.viewer,
        w.p,
    )
    assert updated.get("error") == "memory_not_found", updated
    forgot = await _tool("forget", {"context_id": c, "memory_id": str(w.real)}, w.viewer, w.p)
    assert forgot.get("deleted_count", 0) == 0, forgot
    batch = await _tool("forget", {"context_id": c, "memory_ids": [str(w.real)]}, w.viewer, w.p)
    assert all(r.get("status") != "deleted" for r in batch.get("results", [])), batch

    row = await _row(w, w.real)
    assert row.deleted_at is None and row.content == ORIGINAL_CONTENT


class _LockReached(Exception):
    pass


async def test_a_viewer_is_told_it_is_read_only_before_the_capacity_lock(live) -> None:
    """The role is checked before the capacity lock, which a viewer could not
    act on anyway."""
    w: World = live
    with patch(
        "mcp_server.tools._helpers.ensure_not_capacity_locked",
        AsyncMock(side_effect=_LockReached()),
    ):
        for tool, args in _write_calls(w, w.ctx, w.real):
            if tool in ("remember", "remember_batch", "update_memory", "forget"):
                body = await _tool(tool, args, w.viewer, w.w)
                assert body.get("error") == "permission_denied", (tool, body)
        body = await _tool(
            "rollback_session",
            {"context_id": str(w.ctx), "session_id": "s-1", "dry_run": True},
            w.viewer,
            w.w,
        )
        assert body.get("error") == "permission_denied", body


async def test_the_viewer_check_refuses_a_missing_workspace(live) -> None:
    from mcp_server.tools._helpers import _check_viewer_permission

    w: World = live
    refused = await _check_viewer_permission(w.session, w.member, None, "create memories")
    assert refused is not None
    assert json.loads(refused[0].text)["error"] == "permission_denied"


# ------------------------------------------------------- MCP: member / editor


@pytest.mark.parametrize("who", ["member", "editor"])
async def test_a_member_and_an_editor_still_write(live, who) -> None:
    w: World = live
    user = getattr(w, who)
    c = str(w.ctx)
    remembered = await _tool(
        "remember",
        {
            "context_id": c,
            "summary": f"A {who} note",
            "content": "x",
            "type": "note",
            "dedupe": "off",
        },
        user,
        w.w,
    )
    assert remembered.get("status") == "success", remembered
    new_id = remembered["memory_id"]

    updated = await _tool(
        "update_memory", {"context_id": c, "memory_id": str(w.real), "importance": 0.4}, user, w.w
    )
    assert updated.get("status") == "success", updated
    assert (await _row(w, w.real)).importance == pytest.approx(0.4)

    forgot = await _tool("forget", {"context_id": c, "memory_id": new_id}, user, w.w)
    assert forgot.get("deleted_count") == 1, forgot


async def test_a_member_in_another_workspace_session_writes_and_the_edge_lands_in_the_context_workspace(
    live,
) -> None:
    w: World = live
    c = str(w.ctx)
    remembered = await _tool(
        "remember",
        {
            "context_id": c,
            "summary": "Written from another session",
            "content": "x",
            "type": "note",
            "dedupe": "off",
        },
        w.member,
        w.mp,
    )
    assert remembered.get("status") == "success", remembered
    assert (await _row(w, UUID(remembered["memory_id"]))).workspace_id == w.w

    edge_args = {"context_id": c, "source_id": str(w.decoy), "target_id": str(w.real)}
    created = await _tool("create_edge", {**edge_args, "edge_type": "supersedes"}, w.member, w.mp)
    assert created.get("status") == "success", created
    rows = await _edges(w)
    assert [(r.user_id, r.workspace_id, r.context_id) for r in rows] == [(w.member, w.w, w.ctx)]

    updated = await _tool("update_edge", {**edge_args, "weight": 2.5}, w.member, w.mp)
    assert updated.get("status") == "success", updated
    deleted = await _tool("delete_edge", edge_args, w.member, w.mp)
    assert deleted.get("status") == "success", deleted
    assert await _edges(w) == []


# ------------------------------------------------------------- REST: viewer


@contextlib.asynccontextmanager
async def _rest(
    world: World, user_id: str, *, current: UUID | None, key: UUID | None = None
) -> AsyncIterator[Any]:
    """An HTTP client on the app, authenticated as ``user_id``.

    Each request gets a session of its own on the test engine; every one is
    closed on the way out, so none outlives the test.
    """
    import httpx

    from api.main import app
    from api.routes import agent_state as agent_state_routes
    from api.routes import context_search_config as search_config_routes
    from api.routes import contexts as context_routes
    from api.routes import feedback as feedback_routes
    from api.routes import memory as memory_routes
    from auth.dependencies import get_user_from_api_key_or_session, require_session_auth

    opened: list[AsyncSession] = []

    async def _db() -> AsyncIterator[AsyncSession]:
        session = world.factory()
        opened.append(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    principal = {
        "user_id": user_id,
        "current_workspace_id": current,
        "api_key_workspace_id": key,
        "auth_type": "api_key" if key else "session",
    }
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    if key is None:
        app.dependency_overrides[require_session_auth] = lambda: principal
    # The dependency the routes hold (``live`` patches the ``db.base`` name).
    for route_get_db in {
        memory_routes.get_db,
        search_config_routes.get_db,
        context_routes.get_db,
        feedback_routes.get_db,
        agent_state_routes.get_db,
    }:
        app.dependency_overrides[route_get_db] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        for session in opened:
            await session.close()


async def test_rest_memory_writes_refuse_a_viewer(live) -> None:
    w: World = live
    async with _rest(w, w.viewer, current=w.w) as c:
        remember = await c.post(
            "/api/v1/memory/remember",
            json={
                "summary": "Viewer note that must not land",
                "content": "x",
                "type": "note",
                "context": {"context_id": str(w.ctx)},
            },
        )
        patch_other = await c.patch(f"/api/v1/memory/{w.real}", json={"content": "changed"})
        patch_own = await c.patch(f"/api/v1/memory/{w.viewer_own}", json={"importance": 0.1})
        patch_private = await c.patch(
            f"/api/v1/memory/{w.viewer_private_mem}", json={"importance": 0.1}
        )
        forget = await c.post("/api/v1/memory/forget", json={"memory_id": str(w.real)})
        bulk = await c.post(
            "/api/v1/memory/forget-bulk",
            json={"context_id": str(w.ctx), "all": True, "dry_run": False},
        )

    assert remember.status_code == 403, remember.text
    assert (patch_other.status_code, patch_own.status_code, patch_private.status_code) == (
        404,
        404,
        404,
    )
    assert forget.status_code == 200 and forget.json()["deleted_count"] == 0, forget.text
    assert bulk.status_code == 403, bulk.text

    for memory_id in (w.real, w.viewer_own, w.viewer_private_mem):
        row = await _row(w, memory_id)
        assert row.deleted_at is None
        assert row.content == ORIGINAL_CONTENT and row.importance == pytest.approx(0.9)
    assert await _live_count(w, w.ctx) == 3


async def test_rest_memory_writes_allow_a_member(live) -> None:
    w: World = live
    async with _rest(w, w.member, current=w.mp) as c:
        patched = await c.patch(f"/api/v1/memory/{w.real}", json={"importance": 0.3})
        forgot = await c.post("/api/v1/memory/forget", json={"memory_id": str(w.decoy)})
    assert patched.status_code == 200, patched.text
    assert (await _row(w, w.real)).importance == pytest.approx(0.3)
    assert forgot.status_code == 200 and forgot.json()["deleted_count"] == 1, forgot.text


# ------------------------------------------------------ workspace-scoped key


async def test_a_workspace_scoped_key_stays_in_its_workspace_in_the_context_tools(live) -> None:
    w: World = live
    c = str(w.ctx)
    renamed = await _tool(
        "update_context", {"context_id": c, "display_name": "renamed"}, w.owner, w.a, key=w.a
    )
    assert renamed.get("error") == "context_not_found", renamed
    deleted = await _tool("delete_context", {"context_id": c}, w.owner, w.a, key=w.a)
    assert deleted.get("error") == "context_not_found", deleted
    search = await _tool(
        "update_search_config", {"context_id": c, "semantic_weight": 0.5}, w.owner, w.a, key=w.a
    )
    assert search.get("error") == "context_not_found", search

    w.session.expunge_all()
    ctx = (await w.session.execute(select(Context).where(Context.id == w.ctx))).scalar_one()
    assert ctx.deleted_at is None and ctx.display_name != "renamed"

    # The same key in its own workspace, and the owner without a key, still work.
    own = await _tool(
        "update_context", {"context_id": str(w.a_ctx), "display_name": "own"}, w.owner, w.a, key=w.a
    )
    assert own.get("status") == "success", own
    unscoped = await _tool(
        "update_context", {"context_id": c, "display_name": "renamed"}, w.owner, w.a
    )
    assert unscoped.get("status") == "success", unscoped


async def test_rest_context_routes_keep_a_scoped_key_in_its_workspace(live) -> None:
    w: World = live
    async with _rest(w, w.owner, current=w.a, key=w.a) as c:
        got = await c.get(f"/api/v1/contexts/{w.ctx}/search-config")
        reset = await c.post(f"/api/v1/contexts/{w.ctx}/search-config/reset")
        own = await c.get(f"/api/v1/contexts/{w.a_ctx}/search-config")
        feedback = await c.post(
            f"/api/v1/contexts/{w.ctx}/feedback",
            json={"memory_id": str(w.real), "helpful": True},
        )
        stats = await c.get(f"/api/v1/contexts/{w.ctx}/memory-stats")
        duplicates = await c.get(f"/api/v1/contexts/{w.ctx}/duplicates")
    assert (got.status_code, reset.status_code) == (404, 404), (got.text, reset.text)
    assert (feedback.status_code, stats.status_code, duplicates.status_code) == (404, 404, 404), (
        feedback.text,
        stats.text,
        duplicates.text,
    )
    assert own.status_code == 200, own.text


async def test_rest_context_writes_refuse_a_viewer_in_its_own_private_context(live) -> None:
    """A workspace viewer is read-only in a private context it created: agent
    state, search settings and deleting the context are refused (403)."""
    w: World = live
    async with _rest(w, w.viewer, current=w.w) as c:
        state = await c.put(f"/api/v1/contexts/{w.viewer_private}/state/k", json={"value": "v"})
        reset = await c.post(f"/api/v1/contexts/{w.viewer_private}/search-config/reset")
        deleted = await c.delete(f"/api/v1/contexts/{w.viewer_private}")
    assert (state.status_code, reset.status_code, deleted.status_code) == (403, 403, 403), (
        state.text,
        reset.text,
        deleted.text,
    )
    w.session.expunge_all()
    ctx = (
        await w.session.execute(select(Context).where(Context.id == w.viewer_private))
    ).scalar_one()
    assert ctx.deleted_at is None


# ------------------------------------------------------ linked identities


async def test_linked_accounts_write_by_their_own_role(live) -> None:
    """#1784: a linked account acts in the linked creator's private context
    as itself — a member writes there, a viewer only reads."""
    from services.identity_link_service import IdentityLinkService

    w: World = live
    owner_private = Context(
        id=uuid4(),
        workspace_id=w.w,
        name=f"op-{uuid4().hex[:6]}",
        created_by=w.owner,
        is_private=True,
    )
    member_private = Context(
        id=uuid4(),
        workspace_id=w.w,
        name=f"mp-{uuid4().hex[:6]}",
        created_by=w.member,
        is_private=True,
    )
    w.session.add_all([owner_private, member_private])
    await w.session.flush()
    owner_private_id, member_private_id = owner_private.id, member_private.id
    note = _memory(w.w, member_private_id, w.member, "The member's private note")
    w.session.add(note)
    editor_row = (
        await w.session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == w.w, WorkspaceMember.user_id == w.editor
            )
        )
    ).scalar_one()
    editor_row.allowed_context_ids = [w.ctx, owner_private_id]
    await w.session.commit()
    note_id = note.id
    links = IdentityLinkService(w.session)
    await links.link(w.owner, w.editor)  # the editor's account is a member of W
    await links.link(w.member, w.viewer)  # the viewer's account is a viewer of W

    item = {"summary": "Written as the linked member", "content": "x", "type": "note"}
    wrote = await _tool(
        "remember", {"context_id": str(owner_private_id), **item, "dedupe": "off"}, w.editor, w.w
    )
    assert wrote.get("status") == "success", wrote

    refused = await _tool(
        "remember",
        {"context_id": str(member_private_id), **item, "dedupe": "off"},
        w.viewer,
        w.w,
    )
    assert refused.get("error") == "permission_denied", refused
    changed = await _tool(
        "update_memory",
        {"context_id": str(member_private_id), "memory_id": str(note_id), "importance": 0.1},
        w.viewer,
        w.w,
    )
    assert changed.get("error") == "permission_denied", changed
    read = await _tool(
        "reference",
        {"context_id": str(member_private_id), "memory_id": str(note_id)},
        w.viewer,
        w.w,
    )
    assert read.get("status") == "success", read
    assert (await _row(w, note_id)).importance == pytest.approx(0.9)


# ------------------------------------------------------------ agent bindings


async def test_an_agent_binding_still_decides_writes(live) -> None:
    """A ``direct`` binding writes; a context bound read-only answers
    context_not_found, not the role refusal."""
    from auth.agent_scope import AgentScope, get_agent_scope, set_agent_scope
    from models.agent import Agent
    from services.agent_binding_service import AgentBindingService

    w: World = live
    second = Context(
        id=uuid4(),
        workspace_id=w.w,
        name=f"c2-{uuid4().hex[:6]}",
        created_by=w.owner,
        is_private=False,
    )
    w.session.add(second)
    await w.session.flush()
    agent = Agent(workspace_id=w.w, name=f"agent-{uuid4().hex[:6]}", owner_user_id=w.owner)
    w.session.add(agent)
    await w.session.flush()
    bindings = AgentBindingService(w.session)
    await bindings.create_binding(
        agent=agent, context_id=w.ctx, created_by=w.owner, write_policy="direct", is_default=True
    )
    await bindings.create_binding(
        agent=agent, context_id=second.id, created_by=w.owner, write_policy="deny"
    )
    await w.session.commit()
    agent_id, second_id = agent.id, second.id

    item = {"summary": "Written by the bound agent", "content": "x", "type": "note"}
    previous = get_agent_scope()
    set_agent_scope(AgentScope(agent_id=agent_id, enforcement_mode="enforce", workspace_id=w.w))
    try:
        wrote = await _tool(
            "remember", {"context_id": str(w.ctx), **item, "dedupe": "off"}, w.owner, w.w
        )
        denied = await _tool(
            "remember", {"context_id": str(second_id), **item, "dedupe": "off"}, w.owner, w.w
        )
    finally:
        set_agent_scope(previous)
    assert wrote.get("status") == "success", wrote
    assert denied.get("error") == "context_not_found", denied
    assert await _live_count(w, second_id) == 0
