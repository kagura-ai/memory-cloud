"""Linked identities address each other's write sessions (#2114), on Postgres.

Accounts A and B are one person (``identity_links.group_id``); C is someone
else. A writes session ``SA`` (and in a window, no session). Through the MCP
handlers, as a non-owner editor:

1. B plans the rollback of A's ``SA``; C gets ``session_not_found``, and so
   does B naming C's session. Rolled back by B, the record is stamped with
   B's own sub and A's next call is ``already_rolled_back`` (one record).
2. ``changes_since(session_id='SA')`` lists A's events for B, nothing for C.
3. ``changes_since(by_me=true)`` and the window mode of ``rollback_session``
   cover A's in-window writes for B.
4. An agent credential of B never reaches A's unbound session.

Driven through the handlers only, so the same file runs against the code
before the fix (where B gets ``session_not_found`` / an empty lane). The
vector store, embeddings and the per-memory permission check are stubbed.
Needs the DB container.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole
from models.auth import Context, IdentityLink, User, Workspace
from models.memory import Memory, MemoryRevision, MemorySessionRollback
from utils.datetime import to_utc_iso, utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class Linked:
    """One workspace / context, A and B linked, C not."""

    workspace_id: uuid.UUID
    context_id: uuid.UUID
    a: str
    b: str
    c: str
    session: AsyncSession

    async def write(self, actor: str, session_id: str | None, agent: uuid.UUID | None = None):
        """A ``created`` API write, the way the API records it."""
        memory_id = uuid.uuid4()
        at = utcnow() - timedelta(seconds=2)
        self.session.add(
            Memory(
                id=memory_id,
                user_id=actor,
                workspace_id=self.workspace_id,
                context_id=self.context_id,
                client="test",
                summary=f"memory {memory_id.hex[:6]}",
                content="body",
                type="note",
                importance=0.5,
                tags=["t"],
                embedding_status="success",
                version=1,
                created_at=at,
                agent_id=agent,
                session_id=session_id,
            )
        )
        await self.session.flush()
        self.session.add(
            MemoryRevision(
                memory_id=memory_id,
                context_id=self.context_id,
                version=1,
                kind="created",
                writer="api",
                actor_user_id=actor,
                agent_id=agent,
                session_id=session_id,
                at=at,
            )
        )
        await self.session.flush()
        return memory_id


@pytest_asyncio.fixture(loop_scope="session")
async def linked(async_engine: AsyncEngine) -> AsyncIterator[Linked]:
    tag = uuid.uuid4().hex[:8]
    a, b, c = f"a-{tag}", f"b-{tag}", f"c-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        for user in (a, b, c):
            session.add(User(email=f"{user}@test.example", user_id=user, role="user"))
        await session.flush()
        group = uuid.uuid4()
        for user in (a, b):
            session.add(IdentityLink(group_id=group, user_id=user, linked_by=a))
        session.add(Workspace(id=ws_id, name=f"ws-{tag}", owner_user_id=a))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="linked", created_by=a))
        await session.commit()
        try:
            yield Linked(ws_id, ctx_id, a, b, c, session)
        finally:
            set_agent_scope(None)
            await session.rollback()
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.execute(delete(User).where(User.user_id.in_((a, b, c))))
            await session.commit()


@pytest.fixture(autouse=True)
def _stubs() -> Any:
    with (
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock()),
        patch("services.sleep.undo.re_embed_memory_to_qdrant", AsyncMock(return_value=None)),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()),
    ):
        yield


async def _rollback(
    w: Linked, user_id: str, agent: uuid.UUID | None = None, **args: Any
) -> dict[str, Any]:
    from mcp_server.tools import session_rollback as tool

    async def fake_get_db() -> AsyncIterator[AsyncSession]:
        yield w.session

    set_agent_scope(AgentScope(agent_id=agent, enforcement_mode="enforce") if agent else None)
    try:
        with (
            patch("db.base.get_db", fake_get_db),
            patch.object(
                tool,
                "_resolve_context",
                AsyncMock(return_value=MagicMock(id=w.context_id, workspace_id=w.workspace_id)),
            ),
            patch.object(tool, "_context_response_fields", lambda _c: {}),
            patch.object(tool, "_log_tool_usage", AsyncMock()),
            patch(
                "services.session_rollback.context_role",
                AsyncMock(return_value=ContextRole.EDITOR),
            ),
        ):
            out = await tool.handle_rollback_session(
                {"context_id": str(w.context_id), **args}, user_id, w.workspace_id
            )
    finally:
        set_agent_scope(None)
    return json.loads(out[0].text)


async def _changes(w: Linked, user_id: str, **args: Any) -> dict[str, Any]:
    from mcp_server.tools import listing

    async def fake_get_db() -> AsyncIterator[AsyncSession]:
        yield w.session

    with (
        patch("db.base.get_db", fake_get_db),
        patch.object(
            listing,
            "_resolve_context_for_read",
            AsyncMock(return_value=MagicMock(id=w.context_id, is_private=False)),
        ),
        patch.object(listing, "_context_response_fields", lambda _c: {}),
        patch.object(listing, "_log_tool_usage", AsyncMock()),
        patch(
            "services.session_rollback.context_role",
            AsyncMock(return_value=ContextRole.EDITOR),
        ),
    ):
        out = await listing.handle_changes_since(
            {
                "context_id": str(w.context_id),
                "since": to_utc_iso(utcnow() - timedelta(hours=1)),
                **args,
            },
            user_id,
            w.workspace_id,
        )
    return json.loads(out[0].text)


def _ids(payload: dict[str, Any], key: str) -> set[str]:
    return {item["memory_id"] for item in payload.get(key, [])}


async def test_a_linked_identity_rolls_back_the_others_session(linked: Linked) -> None:
    w = linked
    a1 = await w.write(w.a, "SA")
    a2 = await w.write(w.a, "SA")
    await w.write(w.c, "SC")
    await w.session.commit()

    plan = await _rollback(w, w.b, session_id="SA")
    assert plan["status"] == "dry_run", plan
    assert _ids(plan, "items") == {str(a1), str(a2)}

    # Everyone else: the uniform refusal.
    stranger = await _rollback(w, w.c, session_id="SA")
    assert stranger["error"] == "session_not_found"
    theirs = await _rollback(w, w.b, session_id="SC")
    assert theirs["error"] == "session_not_found"

    done = await _rollback(w, w.b, session_id="SA", dry_run=False)
    assert done["status"] == "rolled_back", done
    record = await w.session.scalar(
        select(MemorySessionRollback).where(MemorySessionRollback.id == done["rollback_id"])
    )
    assert record is not None and record.principal_user_id == w.b  # B's own sub
    again = await _rollback(w, w.a, session_id="SA", dry_run=False)
    assert again["error"] == "already_rolled_back"
    assert again["rollback_id"] == done["rollback_id"]


async def test_an_agent_credential_never_reaches_the_linked_unbound_session(
    linked: Linked,
) -> None:
    w = linked
    await w.write(w.a, "SA")
    await w.session.commit()
    agent = await _rollback(w, w.b, uuid.uuid4(), session_id="SA")
    assert agent["error"] == "session_not_found"


async def test_the_session_lane_lists_the_linked_identitys_events(linked: Linked) -> None:
    w = linked
    a1 = await w.write(w.a, "SA")
    c1 = await w.write(w.c, None)
    await w.session.commit()

    named = await _changes(w, w.b, session_id="SA")
    assert named["status"] == "success", named
    assert _ids(named, "changes") == {str(a1)}
    assert _ids(await _changes(w, w.c, session_id="SA"), "changes") == set()

    mine = await _changes(w, w.b, by_me=True)
    assert _ids(mine, "changes") == {str(a1)}
    assert str(c1) not in _ids(mine, "changes")


async def test_the_window_covers_the_linked_identitys_writes(linked: Linked) -> None:
    w = linked
    since = utcnow() - timedelta(minutes=5)
    a1 = await w.write(w.a, None)
    b1 = await w.write(w.b, "SB")
    await w.write(w.c, None)
    await w.session.commit()
    await asyncio.sleep(0.05)

    plan = await _rollback(w, w.b, since=to_utc_iso(since))
    assert plan["status"] == "dry_run", plan
    assert _ids(plan, "items") == {str(a1), str(b1)}


async def test_records_both_subs_kept_from_before_the_link_resolve_to_the_callers(
    linked: Linked,
) -> None:
    """Each linked sub may hold a record of one session from before the link:
    the caller's own comes first, never a MultipleResultsFound."""
    w = linked
    await w.write(w.a, "SX")
    await w.write(w.b, "SX")
    records = {}
    for sub in (w.a, w.b):
        row = MemorySessionRollback(
            workspace_id=w.workspace_id,
            context_id=w.context_id,
            session_id="SX",
            principal_user_id=sub,
            requested_by=sub,
            status="in_progress",
            summary={},
        )
        w.session.add(row)
        await w.session.flush()
        records[sub] = row.id
    await w.session.commit()

    for sub in (w.a, w.b):
        plan = await _rollback(w, sub, session_id="SX")
        assert plan["status"] == "dry_run", plan
        assert plan["rollback_id"] == records[sub]
    done = await _rollback(w, w.b, session_id="SX", dry_run=False)
    assert done["status"] == "rolled_back" and done["rollback_id"] == records[w.b]


async def test_an_omitted_until_continues_and_closes_the_linked_unfinished_window(
    linked: Linked,
) -> None:
    """A finished window record of A and an unfinished one of B for the same
    window, both from before the link: A's call without until continues B's
    record (the one ``unfinished_window_until`` named) and closes it, instead
    of landing on A's finished record forever."""
    w = linked
    since = (utcnow() - timedelta(minutes=10)).replace(microsecond=0)
    until = since + timedelta(minutes=5)
    a1 = await w.write(w.a, None)
    await w.session.execute(
        update(MemoryRevision)
        .where(MemoryRevision.memory_id == a1)
        .values(at=since + timedelta(seconds=10))
    )
    records = {}
    for sub, status in ((w.a, "completed"), (w.b, "in_progress")):
        row = MemorySessionRollback(
            workspace_id=w.workspace_id,
            context_id=w.context_id,
            session_id=None,
            principal_user_id=sub,
            requested_by=sub,
            window_since=since,
            window_until=until,
            status=status,
            summary={},
        )
        w.session.add(row)
        await w.session.flush()
        records[sub] = row.id
    # A's only write in the window was already handled by A's record.
    await w.session.execute(
        update(MemoryRevision).where(MemoryRevision.memory_id == a1).values(undone_by=records[w.a])
    )
    await w.session.commit()

    first = await _rollback(w, w.a, since=to_utc_iso(since), dry_run=False)
    assert first["status"] == "rolled_back", first
    assert first["rollback_id"] == records[w.b]
    assert first["rollback_status"] == "completed"
    closed = await w.session.scalar(
        select(MemorySessionRollback.status).where(MemorySessionRollback.id == records[w.b])
    )
    assert closed == "completed"
