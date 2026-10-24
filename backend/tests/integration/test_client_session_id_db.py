"""A client-supplied session id on stateless MCP requests, against Postgres (#2122).

Each call is a stateless (2026-07-28) ``tools/call`` driven through
``handle_stateless_post`` after the auth seam's correlation is built, and the
real tool handlers write and read the database:

1. remember / update_memory / forget sent with
   ``_meta['ai.kagura/sessionId']`` record that session and echo it;
   ``changes_since(session_id=)`` lists the three writes and
   ``rollback_session(session_id=, dry_run=true)`` plans them.
2. The ``Kagura-Session-Id`` header does the same for the whole request.
3. Principal binding: another member naming the same id sees nothing —
   ``changes_since`` is empty and ``rollback_session`` is ``session_not_found``.
4. A client sending no id still writes without a session, and the #2121
   window mode (``by_me`` / ``since``) covers it.

The vector store, the embedding pipeline, the quota service and the member
seat check are stubbed.
Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.correlation import (
    CLIENT_SESSION_META_KEY,
    build_correlation_from_headers,
    set_correlation,
)
from auth.agent_scope import set_agent_scope
from utils.datetime import to_utc_iso, utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

PV_KEY = "io.modelcontextprotocol/protocolVersion"
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"


@dataclass
class World:
    workspace_id: uuid.UUID
    context_id: uuid.UUID
    owner: str
    member: str
    other: str
    session: AsyncSession


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncIterator[World]:
    from auth.workspace_roles import ContextRole
    from models.auth import Context, ContextMember, User, Workspace, WorkspaceMember, WorkspaceRole

    tag = uuid.uuid4().hex[:8]
    owner, member, other = f"owner-{tag}", f"member-{tag}", f"other-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        for user in (owner, member, other):
            session.add(User(email=f"{user}@test.example", user_id=user, role="user"))
        await session.flush()
        session.add(
            Workspace(
                id=ws_id,
                name=f"ws-{tag}",
                plan_name="free",
                owner_user_id=owner,
                daily_api_limit=5000,
                weekly_api_limit=25000,
            )
        )
        await session.flush()
        session.add(WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER))
        for user in (member, other):
            session.add(
                WorkspaceMember(
                    workspace_id=ws_id,
                    user_id=user,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[ctx_id],
                )
            )
        session.add(
            Context(
                id=ctx_id,
                workspace_id=ws_id,
                name=f"sessions-{tag}",
                created_by=owner,
                is_private=False,
                trust_tier="trusted",
            )
        )
        await session.flush()
        for user in (member, other):  # both editors: rollback_session needs the role
            session.add(ContextMember(context_id=ctx_id, user_id=user, role=ContextRole.EDITOR))
        await session.commit()
        try:
            yield World(ws_id, ctx_id, owner, member, other, session)
        finally:
            set_agent_scope(None)
            set_correlation(None)
            await session.rollback()
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


@pytest.fixture
def live(world: World) -> Any:
    """Handlers use the world's session; vectors, embeddings, quota and seats are stubbed."""

    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("db.base.get_db", new=_db),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock()),
        patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()),
        patch("services.sleep.undo.re_embed_memory_to_qdrant", AsyncMock(return_value=None)),
        # The free plan seats one member; seating is not what this tests.
        patch("services.plan_seats.ensure_member_seated", AsyncMock()),
        patch("services.permission_service.ensure_member_seated", AsyncMock()),
        patch("services.context_service.ensure_member_seated", AsyncMock()),
    ):
        yield world


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def body(self) -> dict:
        return json.loads(b"".join(m.get("body", b"") for m in self.messages[1:]))


async def _dispatch(**kwargs: Any) -> Any:
    """The real handlers, without execute_tool_call's rate-limit / usage plumbing."""
    from mcp_server.tools.listing import handle_changes_since
    from mcp_server.tools.memory import handle_forget, handle_remember, handle_update_memory
    from mcp_server.tools.session_rollback import handle_rollback_session

    handler = {
        "remember": handle_remember,
        "update_memory": handle_update_memory,
        "forget": handle_forget,
        "changes_since": handle_changes_since,
        "rollback_session": handle_rollback_session,
    }[kwargs["tool_name"]]
    return await handler(kwargs["arguments"], kwargs["user_id"], kwargs["workspace_id"])


async def _call(
    world: World,
    user_id: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    meta_session: str | None = None,
    header_session: str | None = None,
) -> dict[str, Any]:
    """One stateless tools/call: the auth seam's correlation, then the handler."""
    from mcp_server.transport_stateless import handle_stateless_post

    meta: dict[str, Any] = {PV_KEY: "2026-07-28", CAPS_KEY: {}}
    if meta_session is not None:
        meta[CLIENT_SESSION_META_KEY] = meta_session
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments, "_meta": meta},
    }
    headers = {
        b"mcp-protocol-version": b"2026-07-28",
        b"mcp-method": b"tools/call",
        b"mcp-name": tool.encode(),
    }
    set_correlation(
        build_correlation_from_headers(
            traceparent=None, baggage=None, surface="mcp", client_session_id=header_session
        )
    )
    send = _Recorder()
    try:
        with patch("mcp_server.tools.execute_tool_call", _dispatch):
            await handle_stateless_post(
                send, body, headers, user_id=user_id, workspace_id=world.workspace_id
            )
    finally:
        set_correlation(None)
    reply = send.body
    assert "error" not in reply, reply
    return json.loads(reply["result"]["content"][0]["text"])


async def _writes(world: World, **session: str) -> list[str]:
    """remember ×2, update_memory, forget — all as the member, in one session."""
    ctx = str(world.context_id)
    ids: list[str] = []
    for summary in ("the first client memory", "the second client memory"):
        out = await _call(
            world,
            world.member,
            "remember",
            {"context_id": ctx, "summary": summary, "content": "body", "type": "note"},
            **session,
        )
        assert out["status"] == "success", out
        ids.append(out["memory_id"])
        expected = session.get("meta_session") or session.get("header_session")
        assert out.get("session_id") == expected
    updated = await _call(
        world,
        world.member,
        "update_memory",
        {"context_id": ctx, "memory_id": ids[0], "summary": "the first client memory, edited"},
        **session,
    )
    assert updated["status"] == "success", updated
    assert updated.get("session_id") == expected
    forgot = await _call(
        world, world.member, "forget", {"context_id": ctx, "memory_id": ids[1]}, **session
    )
    assert forgot["status"] == "success", forgot
    assert forgot.get("session_id") == expected
    return ids


def _since() -> str:
    return to_utc_iso(utcnow() - timedelta(minutes=5))


@pytest.mark.parametrize(
    "session",
    [{"meta_session": "client-run-7"}, {"header_session": "client-hdr-9"}],
    ids=["meta", "header"],
)
async def test_a_client_session_is_listed_and_planned(live: World, session: dict) -> None:
    session_id = next(iter(session.values()))
    first, second = await _writes(live, **session)
    ctx = str(live.context_id)

    changes = await _call(
        live,
        live.member,
        "changes_since",
        {"context_id": ctx, "since": _since(), "session_id": session_id},
    )
    assert changes["status"] == "success", changes
    assert [(c["memory_id"], c["kind"]) for c in changes["changes"]] == [
        (first, "created"),
        (second, "created"),
        (first, "updated"),
        (second, "forgotten"),
    ]

    plan = await _call(
        live,
        live.member,
        "rollback_session",
        {"context_id": ctx, "session_id": session_id, "dry_run": True},
    )
    assert plan["status"] == "dry_run", plan
    assert plan["session_id"] == session_id
    assert {i["memory_id"] for i in plan["items"]} == {first, second}


async def test_another_principal_naming_the_id_sees_nothing(live: World) -> None:
    """The id is bound to the caller sub + credential agent, like baggage."""
    await _writes(live, meta_session="shared-run-1")
    ctx = str(live.context_id)

    # The other member sends the same id in _meta and names it explicitly.
    changes = await _call(
        live,
        live.other,
        "changes_since",
        {"context_id": ctx, "since": _since(), "session_id": "shared-run-1"},
        meta_session="shared-run-1",
    )
    assert changes["status"] == "success", changes
    assert changes["changes"] == []
    refused = await _call(
        live,
        live.other,
        "rollback_session",
        {"context_id": ctx, "session_id": "shared-run-1", "dry_run": True},
        meta_session="shared-run-1",
    )
    assert refused["error"] == "session_not_found"

    # The writer's own view is intact.
    mine = await _call(
        live,
        live.member,
        "changes_since",
        {"context_id": ctx, "since": _since(), "session_id": "shared-run-1"},
    )
    assert len(mine["changes"]) == 4


async def test_a_client_without_an_id_uses_the_window_mode(live: World) -> None:
    since = _since()
    first, second = await _writes(live)
    ctx = str(live.context_id)

    mine = await _call(
        live, live.member, "changes_since", {"context_id": ctx, "since": since, "by_me": True}
    )
    assert {c["memory_id"] for c in mine["changes"]} == {first, second}
    plan = await _call(live, live.member, "rollback_session", {"context_id": ctx, "since": since})
    assert plan["status"] == "dry_run", plan
    assert "session_id" not in plan
    assert {i["memory_id"] for i in plan["items"]} == {first, second}
