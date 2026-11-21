"""#2959: a ``reference`` miss names the memories whose ids start like the asked one.

Agents sometimes mis-copy a memory id between a ``recall`` result and the
``reference`` that follows: the first characters are right, the rest is
invented. On a miss, the authenticated MCP ``reference`` now names the live
memories of the named context that the caller could read and whose ids share
at least the first 8 hex characters with the asked id (``similar_ids``,
longest shared part first, at most 3). It never returns one and never picks
one.

Through the real session, on real rows: ``execute_tool_call`` → the handler →
``MemoryService`` → ``PermissionService``. The handler rolls its session back
on a miss, which expires every ORM instance it holds, so this is the test that
shows the hint is built from plain values (a mocked session would not expire
anything). Every id is derived from a fresh ``uuid4`` per test.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from mcp_server.tools._capacity_gate import invalidate_capacity_candidate_cache
from mcp_server.tools._helpers import _mcp_key_workspace_scope
from models.agent import Agent
from models.auth import (
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Context,
    IdentityLink,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from models.memory_access_event import MemoryAccessEvent
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PLAIN_MISS_HELP = "Use recall() to find memories you have access to."


@dataclass
class World:
    session: AsyncSession
    owner: str
    member: str
    linked: str
    ws: UUID
    ctx_a: UUID
    ctx_b: UUID
    ctx_private: UUID
    agent: UUID


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    """Workspace W (pro): the owner, a member, an account linked to the owner;
    shared contexts A and B, a private context of the owner's; an agent whose
    binding on A reads notes only. Committed: the handler rolls back on a miss."""
    from services.agent_binding_service import AgentBindingService
    from services.identity_link_service import IdentityLinkService

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    invalidate_capacity_candidate_cache()
    tag = uuid4().hex[:8]
    owner, member, linked = f"rh-owner-{tag}", f"rh-member-{tag}", f"rh-linked-{tag}"
    users = [owner, member, linked]
    async with factory() as s:
        for uid in users:
            s.add(User(email=f"{uid}@example.test", user_id=uid, name=uid, role="user"))
        await s.flush()
        ws = Workspace(
            id=uuid4(),
            name=f"rh-{tag}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=50000,
            weekly_api_limit=250000,
        )
        s.add(ws)
        await s.flush()

        def ctx(name: str, *, private: bool = False) -> Context:
            return Context(
                id=uuid4(),
                workspace_id=ws.id,
                name=f"{name}-{tag}",
                created_by=owner,
                is_private=private,
            )

        a, b, p = ctx("a"), ctx("b"), ctx("p", private=True)
        s.add_all([a, b, p])
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER),
                WorkspaceMember(
                    workspace_id=ws.id,
                    user_id=member,
                    role=WorkspaceRole.MEMBER,
                    allowed_context_ids=[a.id, b.id, p.id],
                ),
            ]
        )
        agent = Agent(workspace_id=ws.id, name=f"rh-agent-{tag}", owner_user_id=owner)
        s.add(agent)
        await s.flush()
        await AgentBindingService(s).create_binding(
            agent=agent,
            context_id=a.id,
            created_by=owner,
            can_read=True,
            write_policy="direct",
            is_default=True,
            allowed_memory_types=["note"],
        )
        await IdentityLinkService(s).link(owner, linked)
        await s.commit()
        world = World(
            session=s,
            owner=owner,
            member=member,
            linked=linked,
            ws=ws.id,
            ctx_a=a.id,
            ctx_b=b.id,
            ctx_private=p.id,
            agent=agent.id,
        )
        try:
            yield world
        finally:
            # Plain values only from here: the rollback expires every row.
            set_agent_scope(None)
            await s.rollback()
            # The audit writer commits on its own; the table is append-only.
            await s.execute(text("ALTER TABLE memory_access_events DISABLE TRIGGER USER"))
            await s.execute(
                delete(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == world.ws)
            )
            await s.execute(text("ALTER TABLE memory_access_events ENABLE TRIGGER USER"))
            await s.commit()
            for stmt in (
                delete(Memory).where(Memory.workspace_id == world.ws),
                text("DELETE FROM usage_stats WHERE user_id = ANY(:ids)").bindparams(ids=users),
                delete(IdentityLink).where(IdentityLink.user_id.in_(users)),
                delete(Workspace).where(Workspace.id == world.ws),
                delete(User).where(User.user_id.in_(users)),
            ):
                await s.execute(stmt)
                await s.commit()
            invalidate_capacity_candidate_cache()


@pytest.fixture
def live(world: World) -> Iterator[World]:
    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    with (
        patch("db.base.get_db", new=_db),
        patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))),
    ):
        yield world


def _sharing(asked: UUID, shared: int, *, step: int = 1) -> UUID:
    """An id whose first ``shared`` hex characters are ``asked``'s and whose
    next one differs by ``step``; the rest is fresh."""
    differing = format((int(asked.hex[shared], 16) + step) % 16, "x")
    return UUID(asked.hex[:shared] + differing + uuid4().hex[shared + 1 :])


def _with_hex(memory_id: UUID, position: int, char: str) -> UUID:
    h = memory_id.hex
    return UUID(h[:position] + char + h[position + 1 :])


async def _seed(
    w: World,
    memory_id: UUID,
    *,
    context: UUID | None = None,
    author: str | None = None,
    summary: str = "Failover runbook: promote replica B",
    mtype: str = "note",
    deleted: bool = False,
) -> UUID:
    w.session.add(
        Memory(
            id=memory_id,
            workspace_id=w.ws,
            context_id=context or w.ctx_a,
            user_id=author or w.owner,
            summary=summary,
            content="the body nobody asked for",
            type=mtype,
            scope="persistent",
            client="test",
            importance=0.5,
            embedding_status="success",
            tags=[],
            source_type="manual",
            deleted_at=utcnow() if deleted else None,
        )
    )
    await w.session.commit()
    return memory_id


async def _reference(
    w: World,
    memory_id: UUID,
    *,
    context: UUID | None = None,
    agent: bool = False,
) -> tuple[str, dict[str, Any]]:
    from mcp_server.tools import execute_tool_call

    token = _mcp_key_workspace_scope.set(w.ws if agent else None)
    set_agent_scope(
        AgentScope(agent_id=w.agent, enforcement_mode="enforce", workspace_id=w.ws)
        if agent
        else None
    )
    try:
        result = await execute_tool_call(
            "reference",
            {"memory_id": str(memory_id), "context_id": str(context or w.ctx_a)},
            w.owner,
            w.ws,
        )
    finally:
        _mcp_key_workspace_scope.reset(token)
        set_agent_scope(None)
    return result[0].text, json.loads(result[0].text)


def _plain_miss(memory_id: UUID) -> str:
    """The exact text a reference miss answered before #2959."""
    return json.dumps(
        {
            "status": "error",
            "error": "memory_not_found",
            "message": f"Memory not found or you don't have access: {memory_id}",
            "help": _PLAIN_MISS_HELP,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def _stats(w: World, *ids: UUID) -> dict[UUID, tuple]:
    w.session.expunge_all()
    rows = await w.session.execute(
        select(Memory.id, Memory.access_count, Memory.reference_count, Memory.last_used_at).where(
            Memory.id.in_(ids)
        )
    )
    stats = {row.id: (row.access_count, row.reference_count, row.last_used_at) for row in rows}
    await w.session.commit()
    return stats


async def _access_events(w: World) -> list[MemoryAccessEvent]:
    w.session.expunge_all()
    rows = (
        (
            await w.session.execute(
                select(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == w.ws)
            )
        )
        .scalars()
        .all()
    )
    await w.session.commit()
    return list(rows)


def _named(payload: dict[str, Any]) -> list[str]:
    return [entry["memory_id"] for entry in payload.get("similar_ids", [])]


async def test_a_miscopied_id_names_the_memory(live) -> None:
    w: World = live
    meant = await _seed(w, uuid4())
    # The observed shape: the first 18 hex characters right, the rest invented.
    asked = _sharing(meant, 18)
    before = await _stats(w, meant)

    _, payload = await _reference(w, asked)

    assert payload["error"] == "memory_not_found"
    assert payload["message"] == f"Memory not found or you don't have access: {asked}"
    assert payload["similar_ids"] == [
        {"memory_id": str(meant), "summary": "Failover runbook: promote replica B"}
    ]
    assert "memory" not in payload
    assert await _stats(w, meant) == before


async def test_a_candidate_sharing_exactly_8_characters_is_named(live) -> None:
    w: World = live
    meant = await _seed(w, uuid4())

    _, payload = await _reference(w, _sharing(meant, 8))

    assert _named(payload) == [str(meant)]


async def test_the_zero_padded_8_character_form_gets_the_hint(live) -> None:
    w: World = live
    meant = await _seed(w, uuid4())
    # An agent that built an id from a short prefix: <8 hex>-0000-0000-0000-000000000000.
    asked = UUID(meant.hex[:8] + "0" * 24)

    _, payload = await _reference(w, asked)

    assert _named(payload) == [str(meant)]


async def test_two_candidates_are_named_longest_shared_part_first_and_none_is_read(live) -> None:
    w: World = live
    asked = _with_hex(uuid4(), 8, "8")
    eighteen = await _seed(w, _sharing(asked, 18), summary="eighteen shared")
    # Below the asked id and below the 18-character match in id order.
    eight = await _seed(w, _sharing(asked, 8, step=-1), summary="eight shared")
    before = await _stats(w, eighteen, eight)

    _, payload = await _reference(w, asked)

    assert payload["error"] == "memory_not_found"
    assert payload["similar_ids"] == [
        {"memory_id": str(eighteen), "summary": "eighteen shared"},
        {"memory_id": str(eight), "summary": "eight shared"},
    ]
    assert "memory" not in payload
    assert await _stats(w, eighteen, eight) == before


async def test_rows_of_another_context_and_deleted_rows_are_never_named(live) -> None:
    w: World = live
    asked = uuid4()
    await _seed(w, _sharing(asked, 18), context=w.ctx_b)
    await _seed(w, _sharing(asked, 17), deleted=True)

    text_answer, _ = await _reference(w, asked)

    assert text_answer == _plain_miss(asked)


async def test_a_row_the_agent_binding_filters_out_is_never_named_and_leaves_no_event(
    live,
) -> None:
    w: World = live
    asked = uuid4()
    hidden = await _seed(w, _sharing(asked, 18), mtype="decision")
    shown = await _seed(w, _sharing(asked, 8))
    events_before = await _access_events(w)

    _, payload = await _reference(w, asked, agent=True)

    assert _named(payload) == [str(shown)]
    events_after = await _access_events(w)
    assert len(events_after) == len(events_before)
    for event in events_after:
        assert event.memory_id not in (hidden, shown)
        assert json.dumps(event.event_metadata or {}).count(str(hidden)) == 0
    # The agent's direct read of the hidden row is refused: the hint named
    # nothing a reference would not return.
    _, direct = await _reference(w, hidden, agent=True)
    assert direct["error"] == "memory_not_found"


async def test_a_private_context_names_only_what_the_author_rule_allows(live) -> None:
    w: World = live
    asked = uuid4()
    # The member's row in the owner's private context: not the owner's to read.
    await _seed(w, _sharing(asked, 18), context=w.ctx_private, author=w.member)
    # A row an account linked to the owner wrote there is the owner's own (#1784).
    linked_row = await _seed(w, _sharing(asked, 8), context=w.ctx_private, author=w.linked)

    _, payload = await _reference(w, asked, context=w.ctx_private)

    assert _named(payload) == [str(linked_row)]


async def test_a_capacity_locked_workspace_gets_the_plain_answer(live) -> None:
    w: World = live
    meant = await _seed(w, uuid4())
    await _seed(w, uuid4())
    workspace = await w.session.get(Workspace, w.ws)
    assert workspace is not None
    workspace.plan_name = "free"
    workspace.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    await w.session.commit()
    invalidate_capacity_candidate_cache()
    asked = _sharing(meant, 18)

    with patch.object(Workspace, "effective_memory_limit", property(lambda self: 1)):
        # The lock is on: a direct read of the memory answers it.
        _, direct = await _reference(w, meant)
        text_answer, _ = await _reference(w, asked)

    assert direct["error"] == "capacity_locked"
    assert text_answer == _plain_miss(asked)


async def test_nothing_sharing_the_prefix_answers_as_before(live) -> None:
    """Regression guard: the answer of a miss without candidates is unchanged."""
    w: World = live
    neighbour = await _seed(w, uuid4())
    asked = _sharing(neighbour, 0)

    text_answer, _ = await _reference(w, asked)

    assert text_answer == _plain_miss(asked)


async def test_a_found_memory_answers_as_before(live) -> None:
    """Regression guard: a hit never carries the key."""
    w: World = live
    meant = await _seed(w, uuid4())
    await _seed(w, _sharing(meant, 18))

    _, payload = await _reference(w, meant)

    assert payload["status"] == "success"
    assert payload["memory"]["memory_id"] == str(meant)
    assert "similar_ids" not in payload
