"""A workspace-scoped API key reaches its own workspace on the memory-id, file
and tag paths (#963).

On real rows, through ``execute_tool_call`` (MCP) and the FastAPI app (REST).
The owner is a member of two workspaces, W and A; "the A key" is a key scoped
to A, held by that owner:

* memory-id paths (reference, list_revisions, explore, update / PATCH, forget
  by id and by ids): a W memory is not found for the A key and nothing
  changes; an A memory works with the same key; a session and an unscoped key
  keep reaching W;
* in MCP those tools address memories of the declared context only;
* file tools and routes: a ``workspace_id`` of W is refused for the A key the
  way a non-member is refused, and nothing changes; A and a session still work;
* the tag list (REST and MCP): W's contexts are not found for the A key, and
  an agent binding limits it like every other context read;
* ``list_my_bindings`` / ``describe_binding`` show only the A key's workspace;
* REST forget by query without a context is a 422, not a 500.

Embedding, vector store, blob storage and quota calls are stubbed; every
session is on the test database (``db.base`` is pointed at the test engine).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from mcp_server.tools._capacity_gate import invalidate_capacity_candidate_cache
from mcp_server.tools._helpers import _mcp_key_workspace_scope
from models.agent import Agent
from models.auth import APIKey, Context, User, Workspace, WorkspaceMember
from models.file_objects import FileObject
from models.memory import Memory, NeuralMemoryEdge

pytestmark = pytest.mark.asyncio(loop_scope="session")

ORIGINAL_CONTENT = "Failover runbook: promote replica B"
ORIGINAL_IMPORTANCE = 0.9


@dataclass
class World:
    session: AsyncSession
    factory: async_sessionmaker[AsyncSession]
    owner: str
    member: str
    ws: dict[str, UUID] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    mem: dict[str, UUID] = field(default_factory=dict)
    files: dict[str, UUID] = field(default_factory=dict)
    agent: UUID | None = None
    bound_key_id: str = ""


def _workspace(owner: str, tag: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ks-{tag}-{uuid4().hex[:6]}",
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
        importance=ORIGINAL_IMPORTANCE,
        embedding_status="success",
        tags=["runbook", "failover"],
    )


def _file(ws: UUID, ctx: UUID | None, letter: str, owner: str) -> FileObject:
    return FileObject(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        sha256=letter * 64,
        size_bytes=10,
        content_type="text/plain",
        filename=f"{letter}.txt",
        storage_key=f"{ws}/{letter * 2}/{letter * 64}",
        status="uploaded",
        created_by=owner,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    invalidate_capacity_candidate_cache()
    tag = uuid4().hex[:8]
    owner, member = f"ks-owner-{tag}", f"ks-member-{tag}"
    async with factory() as s:
        for uid in (owner, member):
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        w, a = _workspace(owner, "w"), _workspace(owner, "a")
        s.add_all([w, a])
        await s.flush()

        def ctx(name: str, wsid: UUID) -> Context:
            return Context(
                id=uuid4(),
                workspace_id=wsid,
                name=f"{name}-{tag}",
                created_by=owner,
                is_private=False,
            )

        contexts = {"w": ctx("w", w.id), "w2": ctx("w2", w.id), "a": ctx("a", a.id)}
        s.add_all(list(contexts.values()))
        await s.flush()
        cid = {k: v.id for k, v in contexts.items()}
        s.add_all(
            [
                WorkspaceMember(workspace_id=w.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=a.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=w.id,
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[cid["w"], cid["w2"]],
                ),
            ]
        )
        mems = {
            "w": _memory(w.id, cid["w"], owner, "W runbook note"),
            "w_del": _memory(w.id, cid["w"], owner, "W note to keep"),
            "w2": _memory(w.id, cid["w2"], owner, "W second context note"),
            "a": _memory(a.id, cid["a"], owner, "A runbook note"),
            "a_del": _memory(a.id, cid["a"], owner, "A note to delete"),
        }
        s.add_all(list(mems.values()))
        files = {
            "w_ws": _file(w.id, None, "a", owner),
            "w_ctx": _file(w.id, cid["w"], "b", owner),
        }
        s.add_all(list(files.values()))
        await s.flush()
        agent = Agent(workspace_id=w.id, name=f"ks-agent-{tag}", owner_user_id=member)
        s.add(agent)
        await s.flush()
        from services.agent_binding_service import AgentBindingService

        await AgentBindingService(s).create_binding(
            agent=agent,
            context_id=cid["w"],
            created_by=owner,
            can_read=True,
            write_policy="direct",
            is_default=False,
        )
        bound = APIKey(
            user_id=owner,
            name=f"bound-{tag}",
            key_hash=uuid4().hex + uuid4().hex,
            key_prefix="kmc_" + uuid4().hex[:8],
            bound_context_id=cid["w"],
        )
        s.add(bound)
        await s.commit()
        world = World(
            session=s,
            factory=factory,
            owner=owner,
            member=member,
            ws={"w": w.id, "a": a.id},
            ctx=cid,
            mem={k: v.id for k, v in mems.items()},
            files={k: v.id for k, v in files.items()},
            agent=agent.id,
            bound_key_id=bound.public_id,
        )
        try:
            yield world
        finally:
            set_agent_scope(None)
            await s.rollback()
            ids = list(world.ws.values())
            for stmt in (
                delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(ids)),
                delete(FileObject).where(FileObject.workspace_id.in_(ids)),
                delete(APIKey).where(APIKey.user_id.in_([owner, member])),
                delete(Workspace).where(Workspace.id.in_(ids)),
                delete(User).where(User.user_id.in_([owner, member])),
            ):
                await s.execute(stmt)
                await s.commit()
            invalidate_capacity_candidate_cache()


@pytest.fixture
def live(world: World) -> Any:
    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    from services.memory_service import MemoryService

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    storage = MagicMock(
        generate_presigned_get=AsyncMock(return_value="https://example.test/get"),
        generate_presigned_put=AsyncMock(return_value="https://example.test/put"),
    )
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch(
            "services.embedding_service.EmbeddingService",
            return_value=MagicMock(embed=AsyncMock(return_value=[0.1])),
        ),
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
        patch("services.file_storage_service.get_blob_storage", return_value=storage),
        patch(
            "services.file_storage_service.storage_quota_service.release_storage_bytes",
            new=AsyncMock(),
        ),
        patch(
            "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
            new=AsyncMock(),
        ),
    ):
        yield world


async def _tool(
    tool: str,
    args: dict[str, Any],
    user_id: str,
    session_ws: UUID | None,
    *,
    key: UUID | None = None,
    agent: AgentScope | None = None,
) -> dict[str, Any]:
    from mcp_server.tools import execute_tool_call

    token = _mcp_key_workspace_scope.set(key)
    set_agent_scope(agent)
    try:
        result = await execute_tool_call(tool, args, user_id, session_ws)
    finally:
        _mcp_key_workspace_scope.reset(token)
        set_agent_scope(None)
    return json.loads(result[0].text)


async def _row(w: World, mid: UUID) -> Memory:
    w.session.expunge_all()
    return (await w.session.execute(select(Memory).where(Memory.id == mid))).scalar_one()


async def _file_row(w: World, fid: UUID) -> FileObject:
    w.session.expunge_all()
    return (await w.session.execute(select(FileObject).where(FileObject.id == fid))).scalar_one()


async def _files_in(w: World, ws: UUID) -> int:
    w.session.expunge_all()
    return (
        await w.session.execute(
            select(func.count()).select_from(FileObject).where(FileObject.workspace_id == ws)
        )
    ).scalar_one()


async def _untouched(w: World, *keys: str) -> None:
    for key in keys:
        row = await _row(w, w.mem[key])
        assert row.deleted_at is None, key
        assert row.importance == pytest.approx(ORIGINAL_IMPORTANCE), key


@contextlib.asynccontextmanager
async def _rest(w: World, user_id: str, *, current: UUID | None, key: UUID | None, api_key: bool):
    import httpx

    import api.routes.contexts as contexts_routes
    import api.routes.files as files_routes
    import api.routes.memory as memory_routes
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session
    from db.base import get_db as orig_get_db

    opened: list[AsyncSession] = []

    async def _db() -> AsyncIterator[AsyncSession]:
        s = w.factory()
        opened.append(s)
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    principal = {
        "user_id": user_id,
        "current_workspace_id": current,
        "api_key_workspace_id": key,
        "auth_type": "api_key" if api_key else "session",
    }
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    for fn in {orig_get_db, contexts_routes.get_db, files_routes.get_db, memory_routes.get_db}:
        app.dependency_overrides[fn] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        for s in opened:
            await s.close()


# ------------------------------------------------------------ memory-id, MCP


@pytest.mark.parametrize("tool", ["reference", "explore", "list_revisions"])
async def test_mcp_reads_of_another_workspace_are_not_found(live, tool) -> None:
    w: World = live
    body = await _tool(
        tool,
        {"context_id": str(w.ctx["a"]), "memory_id": str(w.mem["w"])},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert body.get("error") == "memory_not_found", body


async def test_mcp_writes_of_another_workspace_change_nothing(live) -> None:
    w: World = live
    a_ctx = str(w.ctx["a"])
    upd = await _tool(
        "update_memory",
        {"context_id": a_ctx, "memory_id": str(w.mem["w"]), "importance": 0.11},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert upd.get("error") == "memory_not_found", upd
    one = await _tool(
        "forget",
        {"context_id": a_ctx, "memory_id": str(w.mem["w_del"])},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert one.get("deleted_count") == 0, one
    many = await _tool(
        "forget",
        {"context_id": a_ctx, "memory_ids": [str(w.mem["w_del"])]},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert [r["status"] for r in many["results"]] == ["skipped"], many
    await _untouched(w, "w", "w_del")


async def test_mcp_the_key_works_in_its_own_workspace(live) -> None:
    w: World = live
    a_ctx = str(w.ctx["a"])
    for tool in ("reference", "explore", "list_revisions"):
        body = await _tool(
            tool,
            {"context_id": a_ctx, "memory_id": str(w.mem["a"])},
            w.owner,
            w.ws["a"],
            key=w.ws["a"],
        )
        assert body.get("status") == "success", (tool, body)
    upd = await _tool(
        "update_memory",
        {"context_id": a_ctx, "memory_id": str(w.mem["a"]), "importance": 0.4},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert upd.get("status") == "success", upd
    fgt = await _tool(
        "forget",
        {"context_id": a_ctx, "memory_id": str(w.mem["a_del"])},
        w.owner,
        w.ws["a"],
        key=w.ws["a"],
    )
    assert fgt.get("deleted_count") == 1, fgt
    assert (await _row(w, w.mem["a"])).importance == pytest.approx(0.4)
    assert (await _row(w, w.mem["a_del"])).deleted_at is not None


async def test_mcp_id_tools_address_the_declared_context_only(live) -> None:
    """No key: a W memory of context ``w`` with context ``w2`` declared."""
    w: World = live
    w2, mid = str(w.ctx["w2"]), str(w.mem["w"])
    for tool in ("reference", "explore", "list_revisions"):
        body = await _tool(tool, {"context_id": w2, "memory_id": mid}, w.owner, w.ws["w"])
        assert body.get("error") == "memory_not_found", (tool, body)
    upd = await _tool(
        "update_memory",
        {"context_id": w2, "memory_id": mid, "importance": 0.2},
        w.owner,
        w.ws["w"],
    )
    assert upd.get("error") == "memory_not_found", upd
    fgt = await _tool(
        "forget", {"context_id": w2, "memory_id": str(w.mem["w_del"])}, w.owner, w.ws["w"]
    )
    assert fgt.get("deleted_count") == 0, fgt
    await _untouched(w, "w", "w_del")
    # The memory's own context, no key, any session workspace: unchanged.
    for session_ws in (w.ws["w"], w.ws["a"], None):
        ref = await _tool(
            "reference", {"context_id": str(w.ctx["w"]), "memory_id": mid}, w.owner, session_ws
        )
        assert ref.get("status") == "success", (session_ws, ref)


# ----------------------------------------------------------- memory-id, REST


async def test_rest_memory_id_routes_of_another_workspace(live) -> None:
    w: World = live
    mid, unknown_id = str(w.mem["w"]), str(uuid4())
    async with _rest(w, w.owner, current=w.ws["a"], key=w.ws["a"], api_key=True) as c:
        ref = await c.post("/api/v1/memory/reference", json={"memory_id": mid})
        unknown = await c.post("/api/v1/memory/reference", json={"memory_id": unknown_id})
        rev = await c.get(f"/api/v1/memory/{mid}/revisions")
        exp = await c.post("/api/v1/memory/explore", json={"memory_id": mid})
        pat = await c.patch(f"/api/v1/memory/{mid}", json={"importance": 0.12})
        fgt = await c.post("/api/v1/memory/forget", json={"memory_id": str(w.mem["w_del"])})
        own = await c.post("/api/v1/memory/reference", json={"memory_id": str(w.mem["a"])})
        own_pat = await c.patch(f"/api/v1/memory/{w.mem['a']}", json={"importance": 0.5})
    assert [r.status_code for r in (ref, unknown, rev, exp, pat)] == [404] * 5
    # The not-found an unknown id gets, the id aside.
    assert ref.text.replace(mid, "ID") == unknown.text.replace(unknown_id, "ID")
    assert fgt.status_code == 200 and fgt.json()["deleted_count"] == 0, fgt.text
    assert own.status_code == 200 and own_pat.status_code == 200, (own.text, own_pat.text)
    await _untouched(w, "w", "w_del")


@pytest.mark.parametrize("api_key", [False, True], ids=["session", "unscoped-key"])
async def test_rest_without_a_key_scope_is_unchanged(live, api_key) -> None:
    w: World = live
    async with _rest(w, w.owner, current=w.ws["a"], key=None, api_key=api_key) as c:
        ref = await c.post("/api/v1/memory/reference", json={"memory_id": str(w.mem["w2"])})
        rev = await c.get(f"/api/v1/memory/{w.mem['w2']}/revisions")
        tags = await c.get(f"/api/v1/contexts/{w.ctx['w']}/tags")
        files = await c.get("/api/v1/files", params={"workspace_id": str(w.ws["w"])})
    assert [r.status_code for r in (ref, rev, tags, files)] == [200] * 4


async def test_rest_forget_by_query_is_a_validation_error(live) -> None:
    w: World = live
    async with _rest(w, w.owner, current=w.ws["w"], key=None, api_key=False) as c:
        r = await c.post("/api/v1/memory/forget", json={"query": "runbook", "k": 5})
    assert r.status_code == 422, r.text
    assert "context_id" in r.text
    await _untouched(w, "w", "w_del")


# --------------------------------------------------------------------- files


async def test_mcp_file_tools_refuse_another_workspace(live) -> None:
    w: World = live
    ws_w = str(w.ws["w"])
    calls = [
        ("list_files", {"workspace_id": ws_w}, "viewer"),
        (
            "get_file_download_url",
            {"file_id": str(w.files["w_ctx"]), "workspace_id": ws_w},
            "viewer",
        ),
        (
            "init_file_upload",
            {
                "filename": "k.txt",
                "content_type": "text/plain",
                "size_bytes": 5,
                "sha256": "f" * 64,
                "workspace_id": ws_w,
            },
            "member",
        ),
        (
            "complete_file_upload",
            {"file_id": str(w.files["w_ws"]), "workspace_id": ws_w, "sha256": "a" * 64},
            "member",
        ),
        ("delete_file", {"file_id": str(w.files["w_ws"]), "workspace_id": ws_w}, "member"),
    ]
    for tool, args, role in calls:
        body = await _tool(tool, args, w.owner, w.ws["a"], key=w.ws["a"])
        assert body.get("error") == "permission_denied", (tool, body)
        assert body.get("your_role") == "not_a_member", (tool, body)
        assert body.get("required_role") == role, (tool, body)
    assert (await _file_row(w, w.files["w_ws"])).deleted_at is None
    assert await _files_in(w, w.ws["w"]) == 2
    # Its own workspace (named or by default), and W without a key: allowed.
    for args in ({"workspace_id": str(w.ws["a"])}, {}):
        own = await _tool("list_files", args, w.owner, w.ws["a"], key=w.ws["a"])
        assert own.get("status") == "success", own
    unscoped = await _tool("list_files", {"workspace_id": ws_w}, w.owner, w.ws["a"])
    assert unscoped.get("status") == "success", unscoped
    assert unscoped.get("count", len(unscoped.get("files", []))) == 2, unscoped


async def test_rest_file_routes_refuse_another_workspace(live) -> None:
    w: World = live
    ws_w = str(w.ws["w"])
    async with _rest(w, w.owner, current=w.ws["a"], key=w.ws["a"], api_key=True) as c:
        listed = await c.get("/api/v1/files", params={"workspace_id": ws_w})
        url = await c.get(
            f"/api/v1/files/{w.files['w_ctx']}/download-url", params={"workspace_id": ws_w}
        )
        deleted = await c.delete(f"/api/v1/files/{w.files['w_ws']}", params={"workspace_id": ws_w})
        own = await c.get("/api/v1/files", params={"workspace_id": str(w.ws["a"])})
    async with _rest(w, f"ks-nobody-{uuid4().hex[:6]}", current=None, key=None, api_key=False) as c:
        outsider = await c.get("/api/v1/files", params={"workspace_id": ws_w})
    assert [r.status_code for r in (listed, url, deleted)] == [403] * 3
    # The answer a non-member of W gets.
    assert listed.json() == outsider.json()
    assert own.status_code == 200, own.text
    assert (await _file_row(w, w.files["w_ws"])).deleted_at is None


# ---------------------------------------------------------------------- tags


async def test_tag_list_of_another_workspace_is_not_found(live) -> None:
    w: World = live
    async with _rest(w, w.owner, current=w.ws["a"], key=w.ws["a"], api_key=True) as c:
        other = await c.get(f"/api/v1/contexts/{w.ctx['w']}/tags", params={"q": "runbook"})
        own = await c.get(f"/api/v1/contexts/{w.ctx['a']}/tags")
    assert other.status_code == 404, other.text
    assert own.status_code == 200 and own.json()["tags"], own.text
    mcp_other = await _tool(
        "list_tags", {"context_id": str(w.ctx["w"])}, w.owner, w.ws["a"], key=w.ws["a"]
    )
    assert mcp_other.get("error") == "context_not_found", mcp_other
    mcp_own = await _tool(
        "list_tags", {"context_id": str(w.ctx["a"])}, w.owner, w.ws["a"], key=w.ws["a"]
    )
    assert mcp_own.get("status") == "success" and mcp_own["tags"], mcp_own
    unscoped = await _tool("list_tags", {"context_id": str(w.ctx["w"])}, w.owner, w.ws["a"])
    assert unscoped.get("status") == "success", unscoped


async def test_tag_list_applies_the_agent_binding(live) -> None:
    """An agent bound to context ``w`` only does not read ``w2``'s tags."""
    w: World = live
    assert w.agent is not None
    scope = AgentScope(agent_id=w.agent, enforcement_mode="enforce", workspace_id=w.ws["w"])
    bound = await _tool(
        "list_tags",
        {"context_id": str(w.ctx["w"])},
        w.member,
        w.ws["w"],
        key=w.ws["w"],
        agent=scope,
    )
    assert bound.get("status") == "success", bound
    unbound = await _tool(
        "list_tags",
        {"context_id": str(w.ctx["w2"])},
        w.member,
        w.ws["w"],
        key=w.ws["w"],
        agent=scope,
    )
    assert unbound.get("error") == "context_not_found", unbound
    # Without the agent scope the member reads both.
    plain = await _tool(
        "list_tags", {"context_id": str(w.ctx["w2"])}, w.member, w.ws["w"], key=w.ws["w"]
    )
    assert plain.get("status") == "success", plain


# ------------------------------------------------------------------ bindings


async def test_bindings_are_listed_for_the_keys_workspace_only(live) -> None:
    w: World = live
    scoped = await _tool("list_my_bindings", {}, w.owner, w.ws["a"], key=w.ws["a"])
    assert scoped.get("status") == "success", scoped
    assert scoped["bindings"] == [], scoped
    by_ctx = await _tool(
        "describe_binding", {"context_id": str(w.ctx["w"])}, w.owner, w.ws["a"], key=w.ws["a"]
    )
    by_key = await _tool(
        "describe_binding", {"key_id": w.bound_key_id}, w.owner, w.ws["a"], key=w.ws["a"]
    )
    assert by_ctx.get("error") == "binding_not_found", by_ctx
    assert by_key.get("error") == "binding_not_found", by_key
    # The W key and an unscoped caller see the W binding.
    for key in (w.ws["w"], None):
        listed = await _tool("list_my_bindings", {}, w.owner, w.ws["w"], key=key)
        assert [b["context_id"] for b in listed["bindings"]] == [str(w.ctx["w"])], listed
        described = await _tool(
            "describe_binding", {"key_id": w.bound_key_id}, w.owner, w.ws["w"], key=key
        )
        assert described.get("status") == "success", described
