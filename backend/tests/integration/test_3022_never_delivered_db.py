"""The never-delivered set on Postgres (#3022).

For one viewer, context and instant the MCP list (``list_memories`` with the
compiled filter), the REST list (``GET /memory/list?never_delivered=true``)
and the count (``count_never_delivered`` and its route) select the same rows:

* every clause of ``services.never_delivered`` on a matrix of rows, the NULL
  cases judged rather than dropped, age counted from ``created_at``;
* a public context has no such rows; the number is per viewer; a private
  context's owner filter and an agent binding apply to all three alike;
* the exempt tags are read per request; the age bound is exclusive;
* a recorded delivery and the web dialog's read take a row out;
* over HTTP: a session reads the count, another person's private context is a
  404, a bearer credential a 403, and the REST list without a context a 422.

Seeds commit (the reference read writes on an isolated session); every test
works in its own workspace and deletes what it committed, by its own users and
workspaces: later files in the same CI shard read whole tables and expect them
empty.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from config.constants import NEVER_DELIVERED_AGE_DAYS
from models.auth import AuditLog, Context, IdentityLink, User, Workspace, WorkspaceMember
from models.memory import EDGE_TYPE_SUPERSEDES, Memory, NeuralMemoryEdge
from repositories.memory import MemoryRepository
from services import exempt_tags as exempt_module
from services.identity_link_service import IdentityLinkService
from services.memory_listing import compile_memory_filters, count_never_delivered, list_memories
from services.memory_service import MemoryService
from services.never_delivered import never_delivered_predicates
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

OLD = timedelta(days=NEVER_DELIVERED_AGE_DAYS + 220)

# Every user and workspace the running test committed.
_committed: dict[str, list] = {"users": [], "workspaces": []}


@pytest.fixture(autouse=True)
async def _delete_committed_rows(db_session: AsyncSession):
    """Delete every row the test committed, by its own users and workspaces."""
    for ids in _committed.values():
        ids.clear()
    yield
    users, workspaces = _committed["users"], _committed["workspaces"]
    db = db_session
    await db.rollback()
    # The link writes an audit row per account.
    await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(users)))
    await db.execute(delete(IdentityLink).where(IdentityLink.user_id.in_(users)))
    await db.execute(delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(workspaces)))
    await db.execute(delete(Memory).where(Memory.workspace_id.in_(workspaces)))
    await db.execute(delete(Context).where(Context.workspace_id.in_(workspaces)))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspaces)))
    await db.execute(update(User).where(User.user_id.in_(users)).values(current_workspace_id=None))
    await db.execute(delete(Workspace).where(Workspace.id.in_(workspaces)))
    await db.execute(delete(User).where(User.user_id.in_(users)))
    await db.commit()
    for ids in _committed.values():
        ids.clear()


@pytest.fixture(autouse=True)
def _default_exempt_tags(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    exempt_module._parse.cache_clear()
    yield
    exempt_module._parse.cache_clear()


async def _user(db: AsyncSession, prefix: str) -> str:
    uid = f"{prefix}_{uuid4().hex[:10]}"
    db.add(
        User(
            user_id=uid,
            email=f"{uid}@example.com",
            name=uid,
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
        )
    )
    await db.flush()
    _committed["users"].append(uid)
    return uid


async def _workspace(db: AsyncSession, owner: str, *members: str) -> UUID:
    ws = Workspace(id=uuid4(), name=f"nd-{uuid4().hex[:8]}", owner_user_id=owner, plan_name="pro")
    db.add(ws)
    await db.flush()
    _committed["workspaces"].append(ws.id)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    for m in members:
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=m, role=WorkspaceRole.ADMIN))
    await db.flush()
    return ws.id


async def _context(db: AsyncSession, ws: UUID, creator: str, *, private: bool) -> UUID:
    ctx = Context(
        id=uuid4(),
        workspace_id=ws,
        name=f"nd_{uuid4().hex[:8]}",
        display_name="Never delivered",
        created_by=creator,
        is_private=private,
    )
    db.add(ctx)
    await db.flush()
    return ctx.id


async def _row(db: AsyncSession, user_id: str, ws: UUID, ctx: UUID, **over: Any) -> UUID:
    values: dict[str, Any] = {
        "id": uuid4(),
        "user_id": user_id,
        "workspace_id": ws,
        "context_id": ctx,
        "summary": over.pop("summary", "row"),
        "content": "content",
        "type": "note",
        "importance": 0.5,
        "confidence": 1.0,
        "client": "pytest",
        "source": "manual",
        "source_type": "manual",
        "created_at": utcnow() - OLD,
        "embedding_status": "success",
        "tags": ["misc"],
    }
    values.update(over)
    mem = Memory(**values)
    db.add(mem)
    await db.flush()
    return mem.id


async def _edge(db: AsyncSession, owner: str, ws: UUID, ctx: UUID, src: UUID, dst: UUID) -> None:
    db.add(
        NeuralMemoryEdge(
            user_id=owner,
            src_id=src,
            dst_id=dst,
            workspace_id=ws,
            context_id=ctx,
            edge_type=EDGE_TYPE_SUPERSEDES,
            weight=1.0,
            confidence=1.0,
            origin="declared",
        )
    )
    await db.flush()


@contextlib.asynccontextmanager
async def _client(db: AsyncSession, principal: dict | None):
    """An HTTP client on the whole app, every request on ``db``.

    ``principal`` stands in for the session (``get_current_user``), so the
    route's own dependencies (``require_session_auth``, the API-key-or-session
    resolver) run for real; None means no session at all.
    """
    import httpx

    import api.routes.contexts as contexts_routes
    import api.routes.memory as memory_routes
    from api.main import app
    from db.base import get_db as orig_get_db

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db

    for fn in {orig_get_db, contexts_routes.get_db, memory_routes.get_db}:
        app.dependency_overrides[fn] = _db
    session = AsyncMock(return_value=principal) if principal is not None else None
    try:
        with contextlib.ExitStack() as stack:
            if session is not None:
                stack.enter_context(patch("auth.dependencies.get_current_user", session))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.clear()


def _principal(user_id: str) -> dict:
    return {"user_id": user_id, "sub": user_id, "email": f"{user_id}@example.com", "role": "user"}


async def _three(
    db: AsyncSession, viewer: str, ctx: UUID, *, private: bool
) -> tuple[set, set, int]:
    """(MCP list ids, REST list ids, count) for ``viewer``."""
    owner = viewer if private else None
    preds = compile_memory_filters({"never_delivered": True}, viewer_user_id=viewer)
    page = await list_memories(db, context_id=ctx, owner_user_id=owner, predicates=preds, limit=100)
    assert page.total == len(page.rows)
    mcp_ids = {r.id for r in page.rows}
    async with _client(db, _principal(viewer)) as c:
        r = await c.get(
            "/api/v1/memory/list",
            params={"context_id": str(ctx), "never_delivered": "true", "limit": 500},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    rest_ids = {UUID(m["id"]) for m in body["memories"]}
    assert body["total"] == len(rest_ids)
    count = await count_never_delivered(
        db, context_id=ctx, owner_user_id=owner, viewer_user_id=viewer
    )
    async with _client(db, _principal(viewer)) as c:
        r = await c.get(f"/api/v1/contexts/{ctx}/never-delivered-count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == count
    assert r.json()["older_than_days"] == NEVER_DELIVERED_AGE_DAYS
    return mcp_ids, rest_ids, count


async def _agree(db, viewer, ctx, *, private=False) -> set:
    mcp_ids, rest_ids, count = await _three(db, viewer, ctx, private=private)
    assert mcp_ids == rest_ids
    assert count == len(mcp_ids)
    return mcp_ids


@pytest.fixture
async def matrix(db_session: AsyncSession):
    """A shared context with one row per clause of the set."""
    db = db_session
    viewer = await _user(db, "ndv")
    linked = await _user(db, "ndl")
    other = await _user(db, "ndo")
    ws = await _workspace(db, viewer, linked, other)
    ctx = await _context(db, ws, viewer, private=False)
    ctx2 = await _context(db, ws, viewer, private=False)
    await IdentityLinkService(db).link(viewer, linked)
    now = utcnow()
    r: dict[str, UUID] = {}

    def add(name: str, **kw: Any):
        return _row(db, kw.pop("user", viewer), ws, kw.pop("ctx", ctx), summary=name, **kw)

    # in
    r["base"] = await add("base")
    r["tags_null"] = await add("tags_null", tags=None)
    r["details_null"] = await add("details_null", details=None)
    r["other_tag"] = await add("other_tag", tags=["status:closed"])
    r["linked"] = await add("linked", user=linked)
    r["updated_yesterday"] = await add("upd", updated_at=now - timedelta(days=1))
    r["older_side"] = await add("older_side")
    # out
    r["young"] = await add("young", created_at=now - timedelta(days=10))
    r["stamped"] = await add("stamped", first_delivered_at=now - timedelta(days=5))
    r["accessed"] = await add("accessed", access_count=1)
    r["referenced"] = await add("referenced", reference_count=1, access_count=1)
    r["used"] = await add("used", last_used_at=now - timedelta(days=300))
    r["pinned"] = await add("pinned", delivery_mode="always")
    r["time"] = await add(
        "time",
        type="time",
        details={"trigger": {"from": "2025-01-01T00:00:00", "until": "2025-01-02T00:00:00"}},
    )
    r["guardrail"] = await add("guardrail", details={"tool_trigger": {"tool": "Bash"}})
    r["resource"] = await add(
        "resource",
        details={"resource_id": "res-1", "doc_id": "doc-1", "version": 3},
        summary_embedding_id=uuid4(),
    )
    r["exempt"] = await add("exempt", tags=["status:open", "x"])
    r["newer_side"] = await add("newer_side")
    r["deleted"] = await add("deleted", deleted_at=now - timedelta(days=1))
    r["other_member"] = await add("other_member", user=other)
    r["other_context"] = await add("other_context", ctx=ctx2)
    await _edge(db, viewer, ws, ctx, r["newer_side"], r["older_side"])
    await db.commit()
    return {"db": db, "viewer": viewer, "linked": linked, "other": other, "ws": ws, "ctx": ctx, **r}


_IN = (
    "base",
    "tags_null",
    "details_null",
    "other_tag",
    "linked",
    "updated_yesterday",
    "older_side",
)


async def test_every_clause_and_the_three_surfaces_agree(matrix):
    m = matrix
    ids = await _agree(m["db"], m["viewer"], m["ctx"])
    assert ids == {m[k] for k in _IN}


async def test_a_public_context_has_no_never_delivered_rows(matrix):
    m = matrix
    db = m["db"]
    await db.execute(update(Context).where(Context.id == m["ctx"]).values(is_public=True))
    await db.commit()
    assert await _agree(db, m["viewer"], m["ctx"]) == set()
    await db.execute(update(Context).where(Context.id == m["ctx"]).values(is_public=False))
    await db.commit()
    assert m["base"] in await _agree(db, m["viewer"], m["ctx"])


async def test_the_number_is_per_viewer(matrix):
    m = matrix
    mine = await _agree(m["db"], m["viewer"], m["ctx"])
    theirs = await _agree(m["db"], m["other"], m["ctx"])
    assert theirs == {m["other_member"]}
    assert mine.isdisjoint(theirs)


async def test_the_exempt_tags_are_read_per_request(matrix, monkeypatch):
    m = matrix
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", "")
    ids = await _agree(m["db"], m["viewer"], m["ctx"])
    assert m["exempt"] in ids
    async with _client(m["db"], _principal(m["viewer"])) as c:
        body = (await c.get(f"/api/v1/contexts/{m['ctx']}/never-delivered-count")).json()
    assert body["exempt_tags"] == []
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", "x,status:closed")
    ids = await _agree(m["db"], m["viewer"], m["ctx"])
    assert m["exempt"] not in ids and m["other_tag"] not in ids
    async with _client(m["db"], _principal(m["viewer"])) as c:
        body = (await c.get(f"/api/v1/contexts/{m['ctx']}/never-delivered-count")).json()
    assert body["exempt_tags"] == ["status:closed", "x"]


async def test_an_agent_binding_shrinks_all_three_together(matrix):
    m = matrix
    db = m["db"]
    await db.execute(update(Memory).where(Memory.id == m["base"]).values(type="secret"))
    await db.commit()
    binding = AsyncMock(return_value=Memory.type != "secret")
    with (
        patch("services.memory_listing.binding_memory_sql_predicate", binding),
        patch("api.routes.memory.binding_memory_sql_predicate", binding),
    ):
        ids = await _agree(db, m["viewer"], m["ctx"])
    assert m["base"] not in ids
    assert ids == {m[k] for k in _IN} - {m["base"]}


async def test_a_recorded_delivery_and_the_dialog_read_take_a_row_out(matrix):
    m = matrix
    db = m["db"]
    await MemoryRepository(db).stamp_delivery([m["base"]])
    await db.commit()
    # The web dialog reads through reference with the default record_access.
    await MemoryService(db).reference(m["tags_null"], m["viewer"])
    await db.commit()
    # A read that records nothing leaves the row in.
    await MemoryService(db).reference(m["details_null"], m["viewer"], record_access=False)
    await db.commit()
    ids = await _agree(db, m["viewer"], m["ctx"])
    assert m["base"] not in ids
    assert m["tags_null"] not in ids
    assert m["details_null"] in ids


async def test_merge_copy_shaped_rows_are_out(db_session):
    db = db_session
    viewer = await _user(db, "ndm")
    ws = await _workspace(db, viewer)
    ctx = await _context(db, ws, viewer, private=False)
    now = utcnow()
    plain = await _row(db, viewer, ws, ctx)
    used = await _row(db, viewer, ws, ctx, last_used_at=now - timedelta(days=200))
    stamped = await _row(db, viewer, ws, ctx, first_delivered_at=now - timedelta(days=250))
    await db.commit()
    ids = await _agree(db, viewer, ctx)
    assert ids == {plain}
    assert used not in ids and stamped not in ids


async def test_a_private_context_scopes_all_three_to_the_owner(db_session):
    db = db_session
    owner = await _user(db, "ndp")
    linked = await _user(db, "ndq")
    ws = await _workspace(db, owner, linked)
    ctx = await _context(db, ws, owner, private=True)
    await IdentityLinkService(db).link(owner, linked)
    mine = await _row(db, owner, ws, ctx)
    via_link = await _row(db, linked, ws, ctx)
    await _row(db, owner, ws, ctx, access_count=2)
    await db.commit()
    assert await _agree(db, owner, ctx, private=True) == {mine, via_link}


async def test_the_age_bound_is_exclusive(db_session):
    db = db_session
    viewer = await _user(db, "nda")
    ws = await _workspace(db, viewer)
    ctx = await _context(db, ws, viewer, private=False)
    now = utcnow().replace(microsecond=0)
    cutoff = now - timedelta(days=NEVER_DELIVERED_AGE_DAYS)
    await _row(db, viewer, ws, ctx, created_at=cutoff)
    older = await _row(db, viewer, ws, ctx, created_at=cutoff - timedelta(seconds=1))
    await db.commit()
    count = await count_never_delivered(
        db, context_id=ctx, owner_user_id=None, viewer_user_id=viewer, now=now
    )
    assert count == 1
    preds = never_delivered_predicates(viewer, now=now)
    rows = (await db.execute(select(Memory.id).where(Memory.context_id == ctx, *preds))).scalars()
    assert set(rows) == {older}


async def test_http_session_bearer_private_and_missing_context(db_session):
    db = db_session
    owner = await _user(db, "ndh")
    stranger = await _user(db, "nds")
    ws = await _workspace(db, owner, stranger)
    private = await _context(db, ws, owner, private=True)
    await _row(db, owner, ws, private)
    await db.commit()
    url = f"/api/v1/contexts/{private}/never-delivered-count"

    async with _client(db, _principal(owner)) as c:
        r = await c.get(url)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    async with _client(db, _principal(stranger)) as c:
        r = await c.get(url)
    assert r.status_code == 404, r.text

    async with _client(db, _principal(owner)) as c:
        r = await c.get(url, headers={"Authorization": "Bearer kagura_" + "0" * 40})
    assert r.status_code == 403, r.text

    async with _client(db, None) as c:
        r = await c.get(url)
    assert r.status_code == 401, r.text

    async with _client(db, _principal(owner)) as c:
        r = await c.get("/api/v1/memory/list", params={"never_delivered": "true"})
    assert r.status_code == 422, r.text
    assert "never_delivered needs context_id" in r.text
