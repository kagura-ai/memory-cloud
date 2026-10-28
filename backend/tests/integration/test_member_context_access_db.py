"""Member context access lists on every context path, on real rows.

The rule (``auth.workspace_roles.reaches_context``, Migration 042): owner and
admin reach every context; a member or viewer reaches exactly what its
``allowed_context_ids`` names; with no list a viewer reaches every shared
context and a member none. Privacy applies on top, a viewer only reads, and a
context-member row is a role inside a reached context, never reach itself.

Workspace W has shared contexts A and B and private ones pO (owner), pM (a
member with no list) and pV (a viewer with no list). The expected answer of
every path is computed from that rule by ``_reach`` / ``_writes`` below, not
hand-listed. Paths go through ``execute_tool_call`` (MCP) and the FastAPI app
(REST). Embedding, vector store, blob storage and quota calls are stubbed.
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole, WorkspaceRole
from mcp_server.tools._capacity_gate import invalidate_capacity_candidate_cache
from mcp_server.tools._helpers import _mcp_key_workspace_scope
from models.agent import Agent
from models.auth import (
    Context,
    ContextMember,
    IdentityLink,
    User,
    Workspace,
    WorkspaceMember,
)
from models.file_objects import FileObject
from models.memory import Memory, NeuralMemoryEdge

pytestmark = pytest.mark.asyncio(loop_scope="session")

IMPORTANCE = 0.9

# Contexts of W: (private, creator caller).
CONTEXTS: dict[str, tuple[bool, str]] = {
    "A": (False, "owner"),
    "B": (False, "owner"),
    "pO": (True, "owner"),
    "pM": (True, "m_null_creator"),
    "pV": (True, "v_null"),
}

# Callers in W: (role, context access list as context keys, or None).
CALLERS: dict[str, tuple[WorkspaceRole, list[str] | None]] = {
    "owner": (WorkspaceRole.OWNER, None),
    "admin": (WorkspaceRole.ADMIN, []),
    "m_null": (WorkspaceRole.MEMBER, None),
    "m_empty": (WorkspaceRole.MEMBER, []),
    "mA": (WorkspaceRole.MEMBER, ["A"]),
    "v_null": (WorkspaceRole.VIEWER, None),
    "v_empty": (WorkspaceRole.VIEWER, []),
    "vA": (WorkspaceRole.VIEWER, ["A"]),
    "linked_m": (WorkspaceRole.MEMBER, ["pO"]),
    "m_null_creator": (WorkspaceRole.MEMBER, None),
}
# Accounts linked to each other (#1784).
LINKED = {"owner", "linked_m"}
# Context-member editor rows (a role inside a context, #165).
CONTEXT_EDITORS = {("mA", "A"), ("m_null", "A")}
ALL_CALLERS = [*CALLERS, "outsider"]


def _list_reaches(role: WorkspaceRole, allowed: list[str] | None, ctx: str) -> bool:
    if role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
        return True
    if allowed is None:
        return role == WorkspaceRole.VIEWER
    return ctx in allowed


def _privacy_ok(caller: str, ctx: str) -> bool:
    private, creator = CONTEXTS[ctx]
    if not private or caller == creator:
        return True
    return caller in LINKED and creator in LINKED


def _reach(caller: str, ctx: str) -> bool:
    if caller == "outsider":
        return False
    role, allowed = CALLERS[caller]
    return _privacy_ok(caller, ctx) and _list_reaches(role, allowed, ctx)


def _writes(caller: str, ctx: str) -> bool:
    return _reach(caller, ctx) and CALLERS[caller][0] != WorkspaceRole.VIEWER


def _owns_privately(caller: str, ctx: str) -> bool:
    """Creator of the private context, or linked to it."""
    private, creator = CONTEXTS[ctx]
    return private and (caller == creator or (caller in LINKED and creator in LINKED))


def _context_role(caller: str, ctx: str) -> ContextRole | None:
    """The context role a reachable caller holds, for context settings."""
    role, _ = CALLERS[caller]
    if role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN) or (
        role == WorkspaceRole.MEMBER and _owns_privately(caller, ctx)
    ):
        return ContextRole.OWNER
    if role == WorkspaceRole.VIEWER:
        return ContextRole.VIEWER
    return ContextRole.EDITOR if (caller, ctx) in CONTEXT_EDITORS else None


@dataclass
class World:
    session: AsyncSession
    factory: async_sessionmaker[AsyncSession]
    u: dict[str, str] = field(default_factory=dict)
    ws: dict[str, UUID] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    mem: dict[str, UUID] = field(default_factory=dict)
    files: dict[str, UUID] = field(default_factory=dict)
    deny_agent: UUID | None = None


def _workspace(owner: str, tag: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"mca-{tag}-{uuid4().hex[:6]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _memory(ws: UUID, ctx: UUID, author: str, summary: str, **kw: Any) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content="Failover runbook: promote replica B",
        type="decision",
        scope="persistent",
        client="test",
        importance=IMPORTANCE,
        embedding_status="success",
        tags=["runbook"],
        **kw,
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
    u = {name: f"mca-{name}-{tag}" for name in ALL_CALLERS}
    async with factory() as s:
        users = {
            name: User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user")
            for name, uid in u.items()
        }
        s.add_all(list(users.values()))
        await s.flush()
        w, x = _workspace(u["owner"], "w"), _workspace(u["outsider"], "x")
        s.add_all([w, x])
        await s.flush()
        for name, user in users.items():
            user.current_workspace_id = x.id if name == "outsider" else w.id
        contexts = {
            key: Context(
                id=uuid4(),
                workspace_id=w.id,
                name=f"{key.lower()}-{tag}",
                created_by=u[creator],
                is_private=private,
            )
            for key, (private, creator) in CONTEXTS.items()
        }
        contexts["X"] = Context(
            id=uuid4(), workspace_id=x.id, name=f"x-{tag}", created_by=u["outsider"]
        )
        s.add_all(list(contexts.values()))
        await s.flush()
        cid = {k: v.id for k, v in contexts.items()}
        for name, (role, allowed) in CALLERS.items():
            s.add(
                WorkspaceMember(
                    workspace_id=w.id,
                    user_id=u[name],
                    role=role,
                    allowed_context_ids=None if allowed is None else [cid[k] for k in allowed],
                )
            )
        s.add(WorkspaceMember(workspace_id=x.id, user_id=u["outsider"], role=WorkspaceRole.OWNER))
        for caller, ctx in CONTEXT_EDITORS:
            s.add(ContextMember(context_id=cid[ctx], user_id=u[caller], role=ContextRole.EDITOR))
        group = uuid4()
        for name in LINKED:
            s.add(IdentityLink(group_id=group, user_id=u[name], linked_by=u["owner"]))
        mems: dict[str, Memory] = {}
        for key, (_, creator) in CONTEXTS.items():
            mems[key] = _memory(w.id, cid[key], u[creator], f"{key} runbook note")
            mems[f"{key}_pin"] = _memory(
                w.id, cid[key], u[creator], f"{key} pinned rule", delivery_mode="always"
            )
        # The author case: a memory mA wrote in B, a context outside its list.
        mems["mA_in_B"] = _memory(w.id, cid["B"], u["mA"], "mA wrote this in B")
        s.add_all(list(mems.values()))
        files = {
            "ws": _file(w.id, None, "a", u["owner"]),
            "A": _file(w.id, cid["A"], "b", u["owner"]),
            "B": _file(w.id, cid["B"], "c", u["owner"]),
        }
        s.add_all(list(files.values()))
        await s.flush()
        deny_agent = Agent(workspace_id=w.id, name=f"mca-deny-{tag}", owner_user_id=u["mA"])
        s.add(deny_agent)
        await s.flush()
        from services.agent_binding_service import AgentBindingService

        await AgentBindingService(s).create_binding(
            agent=deny_agent,
            context_id=cid["A"],
            created_by=u["owner"],
            can_read=True,
            write_policy="deny",
            is_default=False,
        )
        await s.commit()
        world = World(
            session=s,
            factory=factory,
            u=u,
            ws={"w": w.id, "x": x.id},
            ctx=cid,
            mem={k: v.id for k, v in mems.items()},
            files={k: v.id for k, v in files.items()},
            deny_agent=deny_agent.id,
        )
        try:
            yield world
        finally:
            set_agent_scope(None)
            await s.rollback()
            ids = list(world.ws.values())
            user_ids = list(u.values())
            for stmt in (
                delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(ids)),
                delete(FileObject).where(FileObject.workspace_id.in_(ids)),
                delete(IdentityLink).where(IdentityLink.user_id.in_(user_ids)),
                User.__table__.update()
                .where(User.user_id.in_(user_ids))
                .values(current_workspace_id=None),
                delete(Workspace).where(Workspace.id.in_(ids)),
                delete(User).where(User.user_id.in_(user_ids)),
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
    agent: AgentScope | None = None,
) -> dict[str, Any]:
    from mcp_server.tools import execute_tool_call

    token = _mcp_key_workspace_scope.set(None)
    set_agent_scope(agent)
    try:
        result = await execute_tool_call(tool, args, user_id, session_ws)
    finally:
        _mcp_key_workspace_scope.reset(token)
        set_agent_scope(None)
    return json.loads(result[0].text)


def _ws_of(w: World, caller: str) -> UUID:
    return w.ws["x"] if caller == "outsider" else w.ws["w"]


@contextlib.asynccontextmanager
async def _rest(w: World, caller: str, *, api_key_workspace_id: UUID | None = None):
    import httpx

    import api.routes.context_search_config as search_config_routes
    import api.routes.contexts as contexts_routes
    import api.routes.files as files_routes
    import api.routes.memory as memory_routes
    import api.routes.workspaces as workspaces_routes
    from api.main import app
    from auth.dependencies import get_user_from_api_key_or_session, require_session_auth
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
        "user_id": w.u[caller],
        "current_workspace_id": _ws_of(w, caller),
        "api_key_workspace_id": api_key_workspace_id,
        "auth_type": "session" if api_key_workspace_id is None else "api_key",
    }
    app.dependency_overrides[get_user_from_api_key_or_session] = lambda: principal
    app.dependency_overrides[require_session_auth] = lambda: principal
    for fn in {
        orig_get_db,
        contexts_routes.get_db,
        files_routes.get_db,
        memory_routes.get_db,
        search_config_routes.get_db,
        workspaces_routes.get_db,
    }:
        app.dependency_overrides[fn] = _db
    try:
        with (
            patch("api.routes.contexts.get_current_user", AsyncMock(return_value=principal)),
            patch("api.routes.workspaces.get_current_user", AsyncMock(return_value=principal)),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.clear()
        for s in opened:
            await s.close()


async def _row(w: World, mid: UUID) -> Memory:
    w.session.expunge_all()
    return (await w.session.execute(select(Memory).where(Memory.id == mid))).scalar_one()


CASES = [(caller, ctx) for caller in ALL_CALLERS for ctx in CONTEXTS]


def _id(case: tuple[str, str]) -> str:
    return f"{case[0]}-{case[1]}"


# ------------------------------------------------------- reads by context id


@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_context_reads_follow_the_list(live, case) -> None:
    """The read resolver, ``get_context`` and the pinned/guardrail loaders agree."""
    w: World = live
    caller, ctx = case
    cid = str(w.ctx[ctx])
    async with _rest(w, caller) as c:
        statuses = {
            "list": (await c.get("/api/v1/memory/list", params={"context_id": cid})).status_code,
            "context": (await c.get(f"/api/v1/contexts/{cid}")).status_code,
            "stats": (await c.get(f"/api/v1/contexts/{cid}/stats")).status_code,
            "tags": (await c.get(f"/api/v1/contexts/{cid}/tags")).status_code,
            "pinned": (await c.post("/api/v1/memory/pinned", json={"context_id": cid})).status_code,
            "guardrails": (
                await c.post("/api/v1/memory/guardrails", json={"context_id": cid})
            ).status_code,
        }
    want = 200 if _reach(caller, ctx) else 404
    assert statuses == dict.fromkeys(statuses, want), statuses
    upcoming = await _tool("recall_upcoming", {"context_id": cid}, w.u[caller], _ws_of(w, caller))
    if _reach(caller, ctx):
        assert upcoming.get("status") == "success", upcoming
    else:
        assert upcoming.get("error") == "context_not_found", upcoming


# ------------------------------------------------------ writes by context id


@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_remember_follows_the_list(live, case) -> None:
    w: World = live
    caller, ctx = case
    s = w.session
    body = await _tool(
        "remember",
        {
            "context_id": str(w.ctx[ctx]),
            "summary": f"note by {caller} in {ctx}",
            "content": "x",
            "type": "note",
            "dedupe": "off",
        },
        w.u[caller],
        _ws_of(w, caller),
    )
    try:
        async with _rest(w, caller) as c:
            rest = await c.post(
                "/api/v1/memory/remember",
                json={
                    "summary": f"rest note by {caller} in {ctx}",
                    "content": "x",
                    "type": "note",
                    "context": {"context_id": str(w.ctx[ctx])},
                },
            )
        if _writes(caller, ctx):
            assert body.get("status") == "success", body
            assert rest.status_code == 200, rest.text
        elif _reach(caller, ctx):
            assert body.get("error") == "permission_denied", body
            assert rest.status_code == 403, rest.text
        else:
            assert body.get("error") == "context_not_found", body
            assert rest.status_code == 404, rest.text
    finally:
        await s.rollback()
        await s.execute(
            delete(Memory).where(
                Memory.context_id == w.ctx[ctx],
                Memory.summary.in_(
                    [f"note by {caller} in {ctx}", f"rest note by {caller} in {ctx}"]
                ),
            )
        )
        await s.commit()


async def test_rest_remember_refuses_a_context_id_that_is_not_a_uuid(live) -> None:
    w: World = live
    async with _rest(w, "mA") as c:
        r = await c.post(
            "/api/v1/memory/remember",
            json={"summary": "bad id", "content": "x", "context": {"context_id": "not-a-uuid"}},
        )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_context_settings_follow_the_list(live, case) -> None:
    """RBAC: the list first (not found), then the context role (403)."""
    w: World = live
    caller, ctx = case
    cid = str(w.ctx[ctx])
    async with _rest(w, caller) as c:
        stats = (await c.get(f"/api/v1/contexts/{cid}/memory-stats")).status_code
        config = (await c.get(f"/api/v1/contexts/{cid}/search-config")).status_code
    upd = await _tool(
        "update_context",
        {"context_id": cid, "description": f"set by {caller}"},
        w.u[caller],
        _ws_of(w, caller),
    )
    if not _reach(caller, ctx):
        assert (stats, config) == (404, 404), (stats, config)
        assert upd.get("error") == "context_not_found", upd
        return
    role = _context_role(caller, ctx)
    assert stats == (200 if role is not None else 403), stats
    editor = role in (ContextRole.OWNER, ContextRole.EDITOR)
    assert config == (200 if editor else 403), config
    if editor:
        assert upd.get("status") == "success", upd
    else:
        assert upd.get("error") == "permission_denied", upd


# --------------------------------------------------------------- memory ids


@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_memory_ids_follow_the_list(live, case) -> None:
    """REST reference / explore / PATCH / forget by id on the memory's own context."""
    w: World = live
    caller, ctx = case
    s = w.session
    victim = _memory(w.ws["w"], w.ctx[ctx], w.u[CONTEXTS[ctx][1]], f"forget probe {caller}")
    s.add(victim)
    await s.commit()
    mid = str(w.mem[ctx])
    try:
        async with _rest(w, caller) as c:
            ref = await c.post("/api/v1/memory/reference", json={"memory_id": mid})
            exp = await c.post("/api/v1/memory/explore", json={"memory_id": mid})
            pat = await c.patch(f"/api/v1/memory/{mid}", json={"importance": 0.7})
            fgt = await c.post("/api/v1/memory/forget", json={"memory_id": str(victim.id)})
        assert ref.status_code == (200 if _reach(caller, ctx) else 404), ref.text
        assert exp.status_code == (200 if _reach(caller, ctx) else 404), exp.text
        assert pat.status_code == (200 if _writes(caller, ctx) else 404), pat.text
        assert fgt.status_code == 200, fgt.text
        gone = (await _row(w, victim.id)).deleted_at is not None
        assert gone is _writes(caller, ctx), fgt.text
    finally:
        await s.rollback()
        await s.execute(delete(Memory).where(Memory.id == victim.id))
        await s.execute(
            Memory.__table__.update()
            .where(Memory.id == w.mem[ctx])
            .values(importance=IMPORTANCE, deleted_at=None)
        )
        await s.commit()


async def test_the_author_of_a_memory_outside_its_list_is_refused(live) -> None:
    """Writing a memory does not keep its context reachable (MCP and REST)."""
    w: World = live
    mid = str(w.mem["mA_in_B"])
    declared = str(w.ctx["A"])
    caller = w.u["mA"]
    ref = await _tool("reference", {"context_id": declared, "memory_id": mid}, caller, w.ws["w"])
    upd = await _tool(
        "update_memory",
        {"context_id": declared, "memory_id": mid, "importance": 0.2},
        caller,
        w.ws["w"],
    )
    fgt = await _tool("forget", {"context_id": declared, "memory_id": mid}, caller, w.ws["w"])
    assert ref.get("error") == "memory_not_found", ref
    assert upd.get("error") == "memory_not_found", upd
    assert fgt.get("deleted_count") == 0, fgt
    async with _rest(w, "mA") as c:
        assert (
            await c.post("/api/v1/memory/reference", json={"memory_id": mid})
        ).status_code == 404
        assert (await c.patch(f"/api/v1/memory/{mid}", json={"importance": 0.2})).status_code == 404
    row = await _row(w, w.mem["mA_in_B"])
    assert row.deleted_at is None and row.importance == pytest.approx(IMPORTANCE)


@pytest.mark.parametrize("caller", ["vA", "v_empty"])
async def test_mcp_memory_ids_of_an_unlisted_context_are_not_found_for_a_viewer(
    live, caller
) -> None:
    w: World = live
    body = await _tool(
        "reference",
        {"context_id": str(w.ctx["A"]), "memory_id": str(w.mem["B"])},
        w.u[caller],
        w.ws["w"],
    )
    assert body.get("error") in ("memory_not_found", "context_not_found"), body
    assert body.get("status") != "success"


# ---------------------------------------------------------------- listings


@pytest.mark.parametrize("caller", list(CALLERS))
async def test_listings_follow_the_list(live, caller) -> None:
    w: World = live
    reachable = {ctx for ctx in CONTEXTS if _reach(caller, ctx)}
    listed = await _tool("list_contexts", {}, w.u[caller], w.ws["w"])
    by_id = {str(w.ctx[k]): k for k in CONTEXTS}
    assert {by_id[c["id"]] for c in listed.get("contexts", [])} == reachable, listed
    async with _rest(w, caller) as c:
        files = await c.get("/api/v1/files", params={"workspace_id": str(w.ws["w"])})
        dash = await c.get(f"/api/v1/workspaces/{w.ws['w']}/contexts/stats")
    assert files.status_code == 200, files.text
    file_keys = {k for k, fid in w.files.items() if str(fid) in {f["id"] for f in files.json()}}
    assert file_keys == {"ws"} | ({"A", "B"} & reachable), file_keys
    if CALLERS[caller][0] == WorkspaceRole.VIEWER:
        assert dash.status_code in (403, 404), dash.text
        return
    assert dash.status_code == 200, dash.text
    shown = {
        by_id[row["context_id"]] for row in dash.json()["contexts"] if row["context_id"] in by_id
    }
    if CALLERS[caller][0] in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
        assert shown == set(CONTEXTS), shown
    else:
        assert shown == reachable, shown


@pytest.mark.parametrize("caller", ["owner", "admin", "mA", "m_null", "outsider"])
async def test_per_context_dashboards_need_reach(live, caller) -> None:
    """Below admin a per-context dashboard needs reach; owners and admins keep
    every row the workspace context stats list for them (others' private ones
    included)."""
    w: World = live
    for ctx in ("A", "B", "pM"):
        async with _rest(w, caller) as c:
            r = await c.get(f"/api/v1/workspaces/{w.ws['w']}/contexts/{w.ctx[ctx]}/usage-timeline")
            missing = await c.get(
                f"/api/v1/workspaces/{w.ws['w']}/contexts/{uuid4()}/usage-timeline"
            )
        if caller == "outsider":
            assert r.status_code in (403, 404), r.text
            continue
        assert missing.status_code == 404, missing.text
        if CALLERS[caller][0] in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
            assert r.status_code == 200, (ctx, r.text)
        else:
            assert r.status_code == (200 if _reach(caller, ctx) else 404), (ctx, r.text)


async def test_context_member_list_shows_only_members_who_reach_the_context(live) -> None:
    w: World = live
    async with _rest(w, "owner") as c:
        r = await c.get(f"/api/v1/contexts/{w.ctx['A']}/members")
    assert r.status_code == 200, r.text
    listed = {row["user_id"] for row in r.json()}
    assert w.u["mA"] in listed
    # m_null holds an editor row on A but reaches nothing.
    assert w.u["m_null"] not in listed
    assert w.u["v_null"] in listed and w.u["vA"] in listed
    assert w.u["v_empty"] not in listed


# ------------------------------------------------------- uniform not-found


def _context_surfaces(cid: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("PUT", f"/api/v1/contexts/{cid}", {"description": "x"}),
        ("DELETE", f"/api/v1/contexts/{cid}", None),
        ("DELETE", f"/api/v1/contexts/{cid}/files", None),
        ("GET", f"/api/v1/contexts/{cid}/members", None),
        ("GET", f"/api/v1/contexts/{cid}/memory-stats", None),
        ("GET", f"/api/v1/contexts/{cid}/duplicates", None),
        ("GET", f"/api/v1/contexts/{cid}/search-config", None),
        (
            "PUT",
            f"/api/v1/contexts/{cid}/search-config",
            {
                "semantic_weight": 0.5,
                "bm25_weight": 0.5,
                "fetch_factor": 3,
                "use_rerank": False,
                "reranker_provider": "voyage",
                "reranker_model": "rerank-2",
            },
        ),
        ("POST", f"/api/v1/contexts/{cid}/search-config/reset", None),
    ]


@pytest.mark.parametrize(("caller", "ctx"), [("outsider", "A"), ("mA", "B"), ("m_null", "A")])
async def test_an_unreachable_context_answers_like_a_missing_one(live, caller, ctx) -> None:
    w: World = live
    real, random = str(w.ctx[ctx]), str(uuid4())
    async with _rest(w, caller) as c:
        for (method, path, body), (_, rpath, _) in zip(
            _context_surfaces(real), _context_surfaces(random), strict=True
        ):
            a = await c.request(method, path, json=body)
            b = await c.request(method, rpath, json=body)
            assert a.status_code == b.status_code == 404, (method, path, a.text, b.text)
            assert a.text.replace(real, "ID") == b.text.replace(random, "ID"), (path, a.text)
    for tool, extra in (
        ("update_context", {"description": "x"}),
        ("delete_context", {}),
        ("update_search_config", {"semantic_weight": 0.5}),
    ):
        a = await _tool(tool, {"context_id": real, **extra}, w.u[caller], _ws_of(w, caller))
        b = await _tool(tool, {"context_id": random, **extra}, w.u[caller], _ws_of(w, caller))
        assert a.get("error") == b.get("error") == "context_not_found", (tool, a, b)
        assert json.dumps(a).replace(real, "ID") == json.dumps(b).replace(random, "ID"), tool
    w.session.expunge_all()
    assert (await w.session.get(Context, w.ctx[ctx])).deleted_at is None


# ------------------------------------------------------------- file delete


async def _file_deleted(w: World, key: str) -> bool:
    w.session.expunge_all()
    row = (
        await w.session.execute(select(FileObject).where(FileObject.id == w.files[key]))
    ).scalar_one()
    return row.deleted_at is not None


async def test_delete_file_under_a_deny_binding_is_not_found(live) -> None:
    w: World = live
    deny = AgentScope(agent_id=w.deny_agent, enforcement_mode="enforce", workspace_id=w.ws["w"])
    body = await _tool(
        "delete_file",
        {"file_id": str(w.files["A"]), "workspace_id": str(w.ws["w"])},
        w.u["mA"],
        w.ws["w"],
        agent=deny,
    )
    assert body.get("error") == "not_found", body
    assert not await _file_deleted(w, "A")


@pytest.mark.parametrize("caller", ["m_empty", "m_null", "v_null"])
async def test_delete_file_of_an_unreachable_or_read_only_context(live, caller) -> None:
    w: World = live
    async with _rest(w, caller) as c:
        r = await c.delete(f"/api/v1/files/{w.files['B']}", params={"workspace_id": str(w.ws["w"])})
    assert r.status_code == (403 if _reach(caller, "B") else 404), r.text
    assert not await _file_deleted(w, "B")


async def test_the_owner_still_deletes_a_file_of_a_deleted_context(live) -> None:
    w: World = live
    s = w.session
    from utils.datetime import utcnow

    gone = Context(
        id=uuid4(),
        workspace_id=w.ws["w"],
        name=f"gone-{uuid4().hex[:6]}",
        created_by=w.u["owner"],
        deleted_at=utcnow(),
    )
    s.add(gone)
    await s.flush()
    f = _file(w.ws["w"], gone.id, "d", w.u["owner"])
    s.add(f)
    await s.commit()
    async with _rest(w, "owner") as c:
        r = await c.delete(f"/api/v1/files/{f.id}", params={"workspace_id": str(w.ws["w"])})
    assert r.status_code == 204, r.text
    s.expunge_all()
    assert (await s.get(FileObject, f.id)).deleted_at is not None


# ------------------------------------------------------------- role change


async def _set(w: World, caller: str, *, role: WorkspaceRole, allowed: list[str] | None) -> None:
    s = w.session
    await s.execute(
        WorkspaceMember.__table__.update()
        .where(WorkspaceMember.workspace_id == w.ws["w"], WorkspaceMember.user_id == w.u[caller])
        .values(
            role=role, allowed_context_ids=None if allowed is None else [w.ctx[k] for k in allowed]
        )
    )
    await s.commit()


async def _member(w: World, caller: str) -> WorkspaceMember:
    w.session.expunge_all()
    return (
        await w.session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == w.ws["w"], WorkspaceMember.user_id == w.u[caller]
            )
        )
    ).scalar_one()


async def _list_status(w: World, caller: str, ctx: str) -> int:
    async with _rest(w, caller) as c:
        return (
            await c.get("/api/v1/memory/list", params={"context_id": str(w.ctx[ctx])})
        ).status_code


@pytest.mark.parametrize(
    ("caller", "start", "new_role", "kept", "reads"),
    [
        # A demoted member keeps its list and reads only that.
        ("mA", (WorkspaceRole.MEMBER, ["A"]), WorkspaceRole.VIEWER, ["A"], {"A"}),
        # A member with no list becomes a viewer that reaches nothing.
        ("m_null", (WorkspaceRole.MEMBER, None), WorkspaceRole.VIEWER, [], set()),
        # A promoted viewer keeps its list.
        ("vA", (WorkspaceRole.VIEWER, ["A"]), WorkspaceRole.MEMBER, ["A"], {"A"}),
        # An admin demoted to member finds its stored value: no list = nothing.
        ("admin", (WorkspaceRole.ADMIN, None), WorkspaceRole.MEMBER, None, set()),
        ("admin", (WorkspaceRole.ADMIN, ["B"]), WorkspaceRole.MEMBER, ["B"], {"B"}),
    ],
)
async def test_a_role_change_keeps_the_list(live, caller, start, new_role, kept, reads) -> None:
    from services.workspace_service import WorkspaceService

    w: World = live
    original = CALLERS[caller]
    await _set(w, caller, role=start[0], allowed=start[1])
    try:
        await WorkspaceService(w.session).update_member_role(w.ws["w"], w.u[caller], new_role)
        row = await _member(w, caller)
        want = None if kept is None else [w.ctx[k] for k in kept]
        assert row.role == new_role and row.allowed_context_ids == want
        for ctx in ("A", "B"):
            assert await _list_status(w, caller, ctx) == (200 if ctx in reads else 404), ctx
        if new_role == WorkspaceRole.MEMBER and "A" in reads:
            body = await _tool(
                "remember",
                {
                    "context_id": str(w.ctx["A"]),
                    "summary": f"after the role change {caller}",
                    "content": "x",
                    "type": "note",
                    "dedupe": "off",
                },
                w.u[caller],
                w.ws["w"],
            )
            assert body.get("status") == "success", body
    finally:
        await w.session.rollback()
        await w.session.execute(
            delete(Memory).where(Memory.summary == f"after the role change {caller}")
        )
        await w.session.commit()
        await _set(w, caller, role=original[0], allowed=original[1])
