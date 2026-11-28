"""The total memory cap refusal names the caller's never-delivered count (#3022), on Postgres.

A workspace at its (patched, small) effective memory limit holds old,
never-delivered memories of the caller and of another member. A refused write
answers with ``never_delivered_count`` equal to the ``total`` of the caller's
own ``never_delivered`` list for the target context, on MCP ``remember``,
``remember_batch``, ``update_memory``'s create half and REST
``POST /memory/remember``; in a private context and under an agent binding
too. The count runs after the refused transaction was rolled back: the
workspace row lock is free while it runs. A replacement by external_id at
the limit is no new memory and is not refused.

The tests commit, so each deletes what it wrote, by its own ids: later files
in the same CI shard read whole tables and expect them empty.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from config.constants import NEVER_DELIVERED_AGE_DAYS
from models.auth import Context, User, Workspace, WorkspaceMember
from models.deferred_point_delete import DeferredPointDelete
from models.memory import Memory
from models.schemas import RememberRequest
from services import exempt_tags as exempt_module
from services import memory_cap_refusal as mcr
from services import quota_service as qs
from services.memory_listing import count_never_delivered
from services.memory_service import MemoryService
from services.plan_headroom import never_delivered_sentence
from utils.datetime import utcnow
from utils.exceptions import QuotaExceededError

pytestmark = pytest.mark.asyncio(loop_scope="session")

OLD = timedelta(days=NEVER_DELIVERED_AGE_DAYS + 30)

# What the running test committed, by its own ids.
_committed: dict[str, list] = {"users": [], "workspaces": []}


@pytest.fixture(autouse=True)
async def _delete_committed_rows(db_session: AsyncSession):
    """Delete every row the test committed, by its own ids."""
    for ids in _committed.values():
        ids.clear()
    yield
    await db_session.rollback()
    workspaces = _committed["workspaces"]
    # A forget queues its row's point delete; the rows' revisions cascade.
    await db_session.execute(
        delete(DeferredPointDelete).where(
            DeferredPointDelete.memory_id.in_(
                select(Memory.id).where(Memory.workspace_id.in_(workspaces))
            )
        )
    )
    await db_session.execute(delete(Memory).where(Memory.workspace_id.in_(workspaces)))
    await db_session.execute(delete(Context).where(Context.workspace_id.in_(workspaces)))
    await db_session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspaces))
    )
    await db_session.execute(delete(Workspace).where(Workspace.id.in_(workspaces)))
    await db_session.execute(delete(User).where(User.user_id.in_(_committed["users"])))
    await db_session.commit()
    for ids in _committed.values():
        ids.clear()


@pytest.fixture(autouse=True)
def _default_exempt_tags(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    exempt_module._parse.cache_clear()
    yield
    exempt_module._parse.cache_clear()


@pytest.fixture
def at_the_limit(monkeypatch):
    """Every workspace's effective memory limit is 1: any live row refuses the next."""

    async def _limit_one(self, workspace_id):
        return {"memory_limit": 1}

    monkeypatch.setattr(qs.EffectiveQuotaService, "get_effective_quotas", _limit_one)


async def _user(db: AsyncSession, prefix: str) -> str:
    uid = f"{prefix}_{uuid4().hex[:10]}"
    _committed["users"].append(uid)
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
    return uid


async def _row(db: AsyncSession, user_id: str, ws: UUID, ctx: UUID, **over: Any) -> UUID:
    values: dict[str, Any] = {
        "id": uuid4(),
        "user_id": user_id,
        "workspace_id": ws,
        "context_id": ctx,
        "summary": "an old memory",
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
    db.add(Memory(**values))
    await db.flush()
    return values["id"]


async def _world(db: AsyncSession, *, private: bool = False) -> tuple[str, UUID, UUID]:
    """Caller + another member; the caller has 3 never-delivered rows in the context.

    Also in the context: the caller's delivered row, the caller's new row, a
    ``secret`` row of the caller (in the set unless a binding hides it) and
    two old rows of the other member (never in the caller's count).
    """
    caller = await _user(db, "cap_caller")
    other = await _user(db, "cap_other")
    ws = Workspace(id=uuid4(), name=f"cap-{uuid4().hex[:8]}", owner_user_id=caller, plan_name="pro")
    _committed["workspaces"].append(ws.id)
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=caller, role=WorkspaceRole.OWNER))
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=other, role=WorkspaceRole.ADMIN))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"cap_{uuid4().hex[:8]}",
        created_by=caller,
        is_private=private,
    )
    db.add(ctx)
    await db.flush()
    for _ in range(2):
        await _row(db, caller, ws.id, ctx.id)
    await _row(db, caller, ws.id, ctx.id, type="secret")
    await _row(db, caller, ws.id, ctx.id, first_delivered_at=utcnow() - OLD)
    await _row(db, caller, ws.id, ctx.id, created_at=utcnow())
    if not private:
        for _ in range(2):
            await _row(db, other, ws.id, ctx.id)
    await db.commit()
    return caller, ws.id, ctx.id


async def _listed_total(db: AsyncSession, caller: str, ctx: UUID, *, private: bool) -> int:
    total = await count_never_delivered(
        db, context_id=ctx, owner_user_id=caller if private else None, viewer_user_id=caller
    )
    await db.rollback()
    return total


def _get_db(db: AsyncSession):
    async def get_db() -> AsyncIterator[AsyncSession]:
        yield db

    return get_db


async def _mcp_remember(db: AsyncSession, caller: str, ws: UUID, ctx: UUID) -> dict:
    from mcp_server.tools.memory import handle_remember

    with (
        patch("db.base.get_db", new=_get_db(db)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
    ):
        (block,) = await handle_remember(
            {
                "context_id": str(ctx),
                "summary": "one memory too many",
                "content": "c",
                "type": "note",
            },
            caller,
            ws,
        )
    return json.loads(block.text)


@pytest.mark.parametrize("private", [False, True])
async def test_mcp_remember_names_the_callers_list_total(db_session, at_the_limit, private):
    caller, ws, ctx = await _world(db_session, private=private)
    expected = await _listed_total(db_session, caller, ctx, private=private)
    assert expected == 3  # two notes and the secret; not the delivered, new or other's rows

    payload = await _mcp_remember(db_session, caller, ws, ctx)

    assert payload["error"] == "quota_exceeded"
    assert payload["quota_type"] == "memories"
    assert payload["gate"] == "quota"
    assert payload["never_delivered_count"] == expected
    assert payload["remedies"][0] == "cleanup"
    assert payload["message"].endswith(
        "plan + addons). 3 of your memories in this context are over "
        f"{NEVER_DELIVERED_AGE_DAYS} days old and were never "
        "delivered."
    )
    # Nothing was written.
    stored = await db_session.scalar(
        select(Memory.id).where(Memory.summary == "one memory too many", Memory.context_id == ctx)
    )
    assert stored is None


async def test_an_agent_binding_scopes_the_count_like_the_list(db_session, at_the_limit):
    caller, ws, ctx = await _world(db_session)
    binding = AsyncMock(return_value=Memory.type != "secret")
    with patch("services.memory_listing.binding_memory_sql_predicate", binding):
        expected = await _listed_total(db_session, caller, ctx, private=False)
        payload = await _mcp_remember(db_session, caller, ws, ctx)

    assert expected == 2
    assert payload["never_delivered_count"] == expected


async def test_the_workspace_row_lock_is_free_while_the_count_runs(
    db_session, async_engine, at_the_limit
):
    caller, ws, ctx = await _world(db_session)
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    real = mcr.count_never_delivered
    locked: list[bool] = []

    async def count_after_taking_the_lock(db, **kwargs):
        async with maker() as other:
            row = await other.execute(
                text("SELECT id FROM workspaces WHERE id = :id FOR UPDATE NOWAIT"), {"id": ws}
            )
            locked.append(row.scalar_one() == ws)
            await other.rollback()
        return await real(db, **kwargs)

    with patch.object(mcr, "count_never_delivered", new=count_after_taking_the_lock):
        payload = await _mcp_remember(db_session, caller, ws, ctx)

    assert locked == [True]
    assert payload["never_delivered_count"] == 3


async def test_remember_batch_reads_the_count_once(db_session, at_the_limit):
    from mcp_server.tools.batch import handle_remember_batch

    caller, ws, ctx = await _world(db_session)
    real = mcr.count_never_delivered
    calls: list[int] = []

    async def counted(db, **kwargs):
        calls.append(1)
        return await real(db, **kwargs)

    items = [
        {"summary": f"batch item {i} summary", "content": "c", "type": "note"} for i in range(3)
    ]
    with (
        patch("db.base.get_db", new=_get_db(db_session)),
        patch("mcp_server.tools.batch._log_tool_usage", new=AsyncMock()),
        patch.object(mcr, "count_never_delivered", new=counted),
    ):
        (block,) = await handle_remember_batch({"context_id": str(ctx), "items": items}, caller, ws)
    payload = json.loads(block.text)

    assert calls == [1]
    assert payload["error"] == "batch_failed"
    assert payload["never_delivered_count"] == 3
    assert [r["error"] for r in payload["results"]] == ["quota_exceeded"] * 3


async def test_rest_remember_names_the_same_number(db_session, at_the_limit):
    from api.routes.memory import remember

    caller, ws, ctx = await _world(db_session)
    expected = await _listed_total(db_session, caller, ctx, private=False)

    with pytest.raises(QuotaExceededError) as raised:
        await remember(
            RememberRequest(
                summary="one memory too many",
                content="c",
                type="note",
                context={"context_id": str(ctx)},
            ),
            {"user_id": caller, "current_workspace_id": ws},
            MemoryService(db_session),
        )

    details = raised.value.details
    assert details["quota_type"] == "memories"
    assert details["never_delivered_count"] == expected == 3
    assert raised.value.message.endswith("plan + addons). " + never_delivered_sentence(3))


# ---------------------------------------------------------------- update_memory at the limit

EXTERNAL_ID = "doc-at-the-limit"


@pytest.fixture
def memory_limit(monkeypatch) -> dict[str, int]:
    """Every workspace's effective memory limit is ``value``, set by the test."""
    limit = {"value": 0}

    async def _limit(self, workspace_id):
        return {"memory_limit": limit["value"]}

    monkeypatch.setattr(qs.EffectiveQuotaService, "get_effective_quotas", _limit)
    return limit


async def _live(db: AsyncSession, ws: UUID) -> int:
    count = await db.scalar(
        select(func.count(Memory.id)).where(Memory.workspace_id == ws, Memory.deleted_at.is_(None))
    )
    await db.rollback()
    return int(count or 0)


async def _mcp_update(db: AsyncSession, caller: str, ws: UUID, ctx: UUID, external_id: str) -> dict:
    from mcp_server.tools.memory import handle_update_memory

    with (
        patch("db.base.get_db", new=_get_db(db)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch.object(MemoryService, "delete_forgotten_points", new=AsyncMock()),
    ):
        (block,) = await handle_update_memory(
            {
                "context_id": str(ctx),
                "external_id": external_id,
                "summary": "the external document, rewritten",
                "content": "new content",
                "type": "note",
            },
            caller,
            ws,
        )
    return json.loads(block.text)


async def test_update_memory_replaces_an_external_id_at_the_limit(db_session, memory_limit):
    caller, ws, ctx = await _world(db_session)
    old = await _row(db_session, caller, ws, ctx, details={"resource_id": EXTERNAL_ID})
    await db_session.commit()
    memory_limit["value"] = at_the_limit = await _live(db_session, ws)

    payload = await _mcp_update(db_session, caller, ws, ctx, EXTERNAL_ID)

    assert "error" not in payload, payload
    assert payload["operation"] == "replaced"
    rows = (
        await db_session.execute(
            select(Memory.id, Memory.deleted_at).where(
                Memory.context_id == ctx, Memory.resource_id == EXTERNAL_ID
            )
        )
    ).all()
    assert {row.id for row in rows if row.deleted_at is None} == {UUID(payload["memory_id"])}
    assert {row.id for row in rows if row.deleted_at is not None} == {old}
    assert await _live(db_session, ws) == at_the_limit


async def test_update_memory_with_a_new_external_id_at_the_limit_is_refused(
    db_session, memory_limit
):
    caller, ws, ctx = await _world(db_session)
    memory_limit["value"] = await _live(db_session, ws)
    expected = await _listed_total(db_session, caller, ctx, private=False)

    payload = await _mcp_update(db_session, caller, ws, ctx, EXTERNAL_ID)

    assert payload["error"] == "quota_exceeded"
    assert (payload["gate"], payload["quota_type"]) == ("quota", "memories")
    assert payload["never_delivered_count"] == expected == 3
    assert payload["remedies"][0] == "cleanup"
    stored = await db_session.scalar(
        select(Memory.id).where(Memory.context_id == ctx, Memory.resource_id == EXTERNAL_ID)
    )
    assert stored is None
