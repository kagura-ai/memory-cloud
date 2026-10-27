"""rollback_session and session provenance against a real Postgres (#2044).

1. Two interleaved sessions in one context, plus a third principal reusing the
   first session's id: the dry run lists exactly the first session's writes,
   ``changes_in_session`` lists the same memories, and the rollback leaves
   every row the other sessions wrote byte-identical.
2. The authz triple: another user's session, the same user's unbound session
   from an agent credential, and an unused id all answer
   ``SessionNotFoundError``; a context owner (user credential) addresses every
   principal's events under the id.
3. A finished rollback refuses a second call (dry run included).
4. The per-call cap: a call stops with ``remaining`` and the next one
   continues; nothing is reversed twice.
5. Per-item SAVEPOINTs: an un-forget whose re-embed fails leaves that
   tombstone as it was, the other items go through, the rollback is
   ``partial`` and is then refused.
6. A later write by another principal skips the memory (``modified_since``).
7. The MCP handler end to end, with the caller's AgentScope deciding the
   principal (cross-principal → ``session_not_found``).
8. The erasure scrub of ``memories.session_id`` and the rollback rows.
9. ``f04_2044_session_provenance`` round trip.
10. #2121 window mode: only the principal's in-window API writes (another
    user, another agent, the same user unbound vs agent-bound, before / after
    the window are untouched); two clients on one credential are both
    covered; continuation under the same record (``until`` omitted too); a
    wider window and session-then-window never reverse an event twice (also
    concurrently); ``no_writes_in_window``; the MCP handler never applies the
    owner override; ``changes_in_session`` without a session (``by_me``);
    erasure of a window row; ``f05_2121_rollback_window`` round trip.

The vector store, the embedding service and the permission check are stubbed.
Every seeded row goes with its workspace. Not executed in the local unit run —
needs the DB container.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole
from models.auth import Context, Workspace
from models.memory import Memory, MemoryRevision, MemorySessionRollback
from services import session_rollback as sr
from services.memory_listing import changes_in_session
from services.write_provenance import SessionPrincipal
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_COLUMNS = (
    "summary",
    "context_summary",
    "content",
    "details",
    "type",
    "importance",
    "tags",
    "version",
    "deleted_at",
    "deleted_by",
    "updated_at",
    "agent_id",
    "session_id",
    "embedding_status",
)


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@dataclass
class World:
    """One workspace / context and helpers that write like the API does."""

    workspace_id: uuid.UUID
    context_id: uuid.UUID
    owner: str
    member: str
    agent: uuid.UUID
    session: AsyncSession
    clock: list[Any] = field(default_factory=list)

    def _tick(self) -> Any:
        # Strictly increasing event times, in write order.
        now = (self.clock[-1] if self.clock else utcnow()) + timedelta(milliseconds=5)
        self.clock.append(now)
        return now

    async def create(
        self, actor: str, session_id: str | None, agent: uuid.UUID | None = None, **fields: Any
    ) -> uuid.UUID:
        memory_id = uuid.uuid4()
        at = self._tick()
        values = {
            "summary": f"memory {memory_id.hex[:6]}",
            "content": "body",
            "type": "note",
            "importance": 0.5,
            "tags": ["t"],
            **fields,
        }
        self.session.add(
            Memory(
                id=memory_id,
                user_id=actor,
                workspace_id=self.workspace_id,
                context_id=self.context_id,
                client="test",
                embedding_status="success",
                version=1,
                created_at=at,
                agent_id=agent,
                session_id=session_id,
                **values,
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

    async def update(
        self,
        memory_id: uuid.UUID,
        actor: str,
        session_id: str | None,
        agent: uuid.UUID | None = None,
        **fields: Any,
    ) -> None:
        memory = await self.session.get(Memory, memory_id)
        assert memory is not None
        at = self._tick()
        prior = {name: getattr(memory, name) for name in fields}
        for name, value in fields.items():
            setattr(memory, name, value)
        memory.version += 1
        memory.updated_at = at
        memory.agent_id, memory.session_id = agent, session_id
        self.session.add(
            MemoryRevision(
                memory_id=memory_id,
                context_id=self.context_id,
                version=memory.version,
                kind="updated",
                writer="api",
                actor_user_id=actor,
                agent_id=agent,
                session_id=session_id,
                prior=json.loads(json.dumps(prior)),
                at=at,
            )
        )
        await self.session.flush()

    async def forget(
        self,
        memory_id: uuid.UUID,
        actor: str,
        session_id: str | None,
        agent: uuid.UUID | None = None,
    ) -> None:
        memory = await self.session.get(Memory, memory_id)
        assert memory is not None
        at = self._tick()
        memory.deleted_at, memory.deleted_by = at, actor
        memory.agent_id, memory.session_id = agent, session_id
        self.session.add(
            MemoryRevision(
                memory_id=memory_id,
                context_id=self.context_id,
                version=memory.version,
                kind="forgotten",
                writer="api",
                actor_user_id=actor,
                agent_id=agent,
                session_id=session_id,
                at=at,
            )
        )
        await self.session.flush()

    async def row(self, memory_id: uuid.UUID) -> dict[str, Any]:
        result = await self.session.execute(
            text(f"SELECT {', '.join(_COLUMNS)} FROM memories WHERE id = :m"),  # noqa: S608
            {"m": memory_id},
        )
        return dict(result.mappings().one())


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine: AsyncEngine) -> AsyncIterator[World]:
    tag = uuid.uuid4().hex[:8]
    owner, member = f"owner-{tag}", f"member-{tag}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    async with _maker(async_engine)() as session:
        session.add(Workspace(id=ws_id, name=f"ws-{tag}", owner_user_id=owner))
        await session.flush()
        session.add(Context(id=ctx_id, workspace_id=ws_id, name="sessions", created_by=owner))
        await session.commit()
        try:
            yield World(
                workspace_id=ws_id,
                context_id=ctx_id,
                owner=owner,
                member=member,
                agent=uuid.uuid4(),
                session=session,
            )
        finally:
            set_agent_scope(None)
            await session.rollback()
            await session.execute(delete(Workspace).where(Workspace.id == ws_id))
            await session.commit()


@pytest.fixture(autouse=True)
def _stubs() -> Any:
    """The vector store, embeddings and the per-memory permission check."""
    with (
        patch(
            "services.permission_service.PermissionService.can_access_memory",
            AsyncMock(return_value=True),
        ),
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock()) as points,
        patch(
            "services.sleep.undo.re_embed_memory_to_qdrant", AsyncMock(return_value=None)
        ) as reembed,
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()) as payload,
    ):
        yield {"points": points, "reembed": reembed, "payload": payload}


def _errors(exc: BaseException) -> str:
    return "internal_error"


def _cid() -> str:
    return "corr-test"


async def _rollback(
    world: World,
    session_id: str,
    principal: SessionPrincipal | None,
    *,
    dry_run: bool,
    time_budget: float | None = None,
) -> sr.RollbackResult:
    return await sr.rollback_session(
        world.session,
        scope=sr.SessionScope(world.context_id, session_id, principal),
        workspace_id=world.workspace_id,
        requested_by=principal.actor_user_id if principal else world.owner,
        dry_run=dry_run,
        classify=_errors,
        new_correlation_id=_cid,
        time_budget=time_budget,
    )


async def _interleaved(world: World) -> dict[str, uuid.UUID]:
    """S1 (owner, no agent), S2 (member) and the member reusing the id S1."""
    w, owner, member = world, world.owner, world.member
    p1 = await w.create(member, None, summary="pre one", tags=["x"])
    p2 = await w.create(member, None, summary="pre two")
    p3 = await w.create(member, None, summary="pre three")
    m1 = await w.create(owner, "S1")
    m2 = await w.create(member, "S2")
    await w.update(p1, owner, "S1", summary="edited by S1")
    m3 = await w.create(member, "S1")  # the same id, another principal
    await w.update(p3, member, "S2", importance=0.9)
    await w.forget(p2, owner, "S1")
    m4 = await w.create(owner, "S1")
    await w.update(m4, owner, "S1", content="second body")
    await w.update(p1, owner, "S1", tags=["y"])
    await w.session.commit()
    return {"p1": p1, "p2": p2, "p3": p3, "m1": m1, "m2": m2, "m3": m3, "m4": m4}


async def test_interleaved_sessions_roll_back_only_the_named_one(
    world: World, _stubs: dict[str, Any]
) -> None:
    ids = await _interleaved(world)
    s1 = SessionPrincipal(world.owner, None)
    untouched = {k: await world.row(ids[k]) for k in ("m2", "m3", "p3")}

    plan = await _rollback(world, "S1", s1, dry_run=True)
    by_memory = {item.memory_id: item for item in plan.items}
    assert set(by_memory) == {ids["m1"], ids["p1"], ids["p2"], ids["m4"]}
    assert by_memory[ids["m1"]].action == sr.ACTION_FORGET
    assert by_memory[ids["m4"]].action == sr.ACTION_FORGET  # created, then edited
    assert by_memory[ids["p1"]].action == sr.ACTION_RESTORE
    assert by_memory[ids["p1"]].values == {"summary": "pre one", "tags": ["x"]}
    assert by_memory[ids["p2"]].action == sr.ACTION_UNFORGET
    assert (plan.events, plan.remaining, plan.rollback_id) == (6, 0, None)
    # A dry run writes nothing.
    assert (await world.session.scalar(select(MemorySessionRollback.id).limit(1))) is None

    page = await changes_in_session(
        world.session,
        context_id=world.context_id,
        session_id="S1",
        principal=s1,
        owner_user_id=None,
        since=world.clock[0] - timedelta(seconds=1),
        until=None,
    )
    assert {c.memory_id for c in page.changes} == set(by_memory)
    assert [c.kind for c in page.changes] == [
        "created",
        "updated",
        "forgotten",
        "created",
        "updated",
        "updated",
    ]
    p2_before = await world.row(ids["p2"])

    result = await _rollback(world, "S1", s1, dry_run=False)
    await world.session.commit()
    assert result.status == "completed" and result.remaining == 0
    assert result.summary["forgotten"] == 2
    assert result.summary["restored"] == 1
    assert result.summary["updated_restored"] == 1
    assert result.errors == []

    for key in ("m1", "m4"):
        row = await world.row(ids[key])
        assert row["deleted_at"] is not None and row["deleted_by"] == world.owner
    p1 = await world.row(ids["p1"])
    assert (p1["summary"], p1["tags"], p1["version"]) == ("pre one", ["x"], 4)
    p2 = await world.row(ids["p2"])
    assert p2["deleted_at"] is None and p2["deleted_by"] is None
    # An un-forget changes no content: updated_at stays (#1852/#2063).
    assert p2["updated_at"] == p2_before["updated_at"]
    assert _stubs["reembed"].await_count == 1
    for key, before in untouched.items():
        assert await world.row(ids[key]) == before, key

    rollback_events = (
        await world.session.execute(
            select(MemoryRevision.memory_id, MemoryRevision.kind, MemoryRevision.writer).where(
                MemoryRevision.writer == "rollback"
            )
        )
    ).all()
    assert sorted((r.kind, r.memory_id == ids["p1"]) for r in rollback_events) == [
        ("forgotten", False),
        ("forgotten", False),
        ("restored", False),
        ("updated", True),
    ]
    # #2266: the restore row names the block it reverted (the lowest version
    # among the session's ``updated`` events on the memory); a non-versioned
    # key, so the row's changed fields read as before
    from services.memory_versions import changed_fields

    restore_prior = await world.session.scalar(
        select(MemoryRevision.prior).where(
            MemoryRevision.memory_id == ids["p1"],
            MemoryRevision.writer == "rollback",
            MemoryRevision.kind == "updated",
        )
    )
    lowest = await world.session.scalar(
        select(func.min(MemoryRevision.version)).where(
            MemoryRevision.memory_id == ids["p1"],
            MemoryRevision.session_id == "S1",
            MemoryRevision.kind == "updated",
        )
    )
    assert restore_prior is not None and lowest is not None
    assert restore_prior["restored_from"] == lowest
    assert changed_fields(restore_prior) == ["summary", "tags"]
    # a forget (the block starts at a ``created`` event) and an un-forget
    # write no ``restored_from``
    others = (
        await world.session.execute(
            select(MemoryRevision.prior).where(
                MemoryRevision.writer == "rollback", MemoryRevision.memory_id != ids["p1"]
            )
        )
    ).scalars()
    assert all("restored_from" not in (prior or {}) for prior in others)
    handled = await world.session.scalar(
        select(func.count()).where(
            MemoryRevision.session_id == "S1",
            MemoryRevision.actor_user_id == world.owner,
            MemoryRevision.undone_by == result.rollback_id,
        )
    )
    assert handled == 6


async def test_session_is_addressed_only_with_its_principal(world: World) -> None:
    ids = await _interleaved(world)
    # Another user naming S2 (the member's), the owner's agent naming the
    # owner's unbound S1, and an unused id: one uniform refusal.
    for session_id, principal in (
        ("S2", SessionPrincipal(world.owner, None)),
        ("S1", SessionPrincipal(world.owner, world.agent)),
        ("never-used", SessionPrincipal(world.owner, None)),
    ):
        with pytest.raises(sr.SessionNotFoundError):
            await _rollback(world, session_id, principal, dry_run=True)
        page = await changes_in_session(
            world.session,
            context_id=world.context_id,
            session_id=session_id,
            principal=principal,
            owner_user_id=None,
            since=world.clock[0] - timedelta(seconds=1),
            until=None,
        )
        assert page.changes == []

    # The member's own S1 is a different session from the owner's S1.
    member_plan = await _rollback(world, "S1", SessionPrincipal(world.member, None), dry_run=True)
    assert {item.memory_id for item in member_plan.items} == {ids["m3"]}

    # A context owner (user credential) names the id: every principal's events.
    owner_plan = await _rollback(world, "S1", None, dry_run=True)
    assert {item.memory_id for item in owner_plan.items} == {
        ids["m1"],
        ids["p1"],
        ids["p2"],
        ids["m3"],
        ids["m4"],
    }


async def test_addressable_principal_owner_override_is_user_credential_only(
    world: World,
) -> None:
    db = world.session
    assert await sr.addressable_principal(db, world.owner, ContextRole.OWNER) is None
    assert await sr.addressable_principal(db, world.member, ContextRole.EDITOR) == (
        SessionPrincipal(world.member, None)
    )
    set_agent_scope(AgentScope(agent_id=world.agent, enforcement_mode="enforce"))
    try:
        assert await sr.addressable_principal(db, world.owner, ContextRole.OWNER) == (
            SessionPrincipal(world.owner, world.agent)
        )
    finally:
        set_agent_scope(None)


async def test_a_finished_rollback_is_refused(world: World) -> None:
    await _interleaved(world)
    s1 = SessionPrincipal(world.owner, None)
    first = await _rollback(world, "S1", s1, dry_run=False)
    await world.session.commit()
    for dry_run in (True, False):
        with pytest.raises(sr.AlreadyRolledBackError) as refused:
            await _rollback(world, "S1", s1, dry_run=dry_run)
        assert refused.value.rollback_id == first.rollback_id
        assert refused.value.status == "completed"
        await world.session.rollback()
    # The context owner's rollback of the id then finds the owner's events
    # handled and only the member's left.
    owner_plan = await _rollback(world, "S1", None, dry_run=True)
    assert len(owner_plan.items) == 1

    # The session writes again after its rollback completed (one MCP session
    # id per connection): the new write is rolled back under the same row.
    later = await world.create(world.owner, "S1")
    await world.session.commit()
    again = await _rollback(world, "S1", s1, dry_run=False)
    await world.session.commit()
    assert (again.rollback_id, again.status) == (first.rollback_id, "completed")
    assert [i.memory_id for i in again.items] == [later]
    assert (await world.row(later))["deleted_at"] is not None


async def test_cap_continues_on_the_next_call(world: World) -> None:
    w = world
    created = [await w.create(w.owner, "BIG") for _ in range(5)]
    await w.session.commit()
    s = SessionPrincipal(w.owner, None)
    with patch.object(sr, "MAX_ROLLBACK_EVENTS_PER_CALL", 2):
        first = await _rollback(w, "BIG", s, dry_run=False)
        await w.session.commit()
        assert (first.status, first.events, first.remaining) == ("in_progress", 2, 3)
        # Newest first.
        assert [i.memory_id for i in first.items] == created[::-1][:2]
        second = await _rollback(w, "BIG", s, dry_run=False)
        await w.session.commit()
        assert (second.status, second.events, second.remaining) == ("in_progress", 2, 1)
        third = await _rollback(w, "BIG", s, dry_run=False)
        await w.session.commit()
    assert (third.status, third.events, third.remaining) == ("completed", 1, 0)
    assert first.rollback_id == second.rollback_id == third.rollback_id
    assert third.totals["forgotten"] == 5 and third.totals["calls"] == 3
    for memory_id in created:
        assert (await w.row(memory_id))["deleted_at"] is not None


async def test_a_failed_item_is_isolated_and_retried_by_the_next_call(
    world: World, _stubs: dict[str, Any]
) -> None:
    w = world
    kept = await w.create(w.member, None)
    broken = await w.create(w.member, None)
    await w.forget(kept, w.owner, "S9")
    await w.forget(broken, w.owner, "S9")
    fresh = await w.create(w.owner, "S9")
    await w.session.commit()

    async def reembed(memory: Any, *_a: Any, **_k: Any) -> None:
        if memory.id == broken:
            raise ConnectionError("vector store down")

    _stubs["reembed"].side_effect = reembed
    s = SessionPrincipal(w.owner, None)
    result = await _rollback(w, "S9", s, dry_run=False)
    await w.session.commit()
    assert result.status == "partial" and result.remaining == 1
    assert result.summary["failed"] == 1 and result.summary["restored"] == 1
    assert result.summary["forgotten"] == 1
    assert result.forgotten_points  # deleted after the commit, by finish()
    _stubs["points"].assert_not_awaited()
    assert len(result.errors) == 1 and str(broken) in result.errors[0]
    assert "corr-test" in result.errors[0]
    assert (await w.row(kept))["deleted_at"] is None
    broken_row = await w.row(broken)
    assert broken_row["deleted_at"] is not None and broken_row["deleted_by"] == w.owner
    assert (await w.row(fresh))["deleted_at"] is not None
    # No restored event survived for the failed item.
    restored_for_broken = await w.session.scalar(
        select(func.count()).where(
            MemoryRevision.memory_id == broken, MemoryRevision.kind == "restored"
        )
    )
    assert restored_for_broken == 0

    # The vector store is back: the next call retries the failed item only.
    _stubs["reembed"].side_effect = None
    retry = await _rollback(w, "S9", s, dry_run=False)
    await w.session.commit()
    assert (retry.status, retry.remaining, retry.events) == ("completed", 0, 1)
    assert [i.memory_id for i in retry.items] == [broken]
    assert retry.rollback_id == result.rollback_id
    assert (await w.row(broken))["deleted_at"] is None
    assert retry.totals["restored"] == 2 and retry.totals["failed"] == 1
    with pytest.raises(sr.AlreadyRolledBackError) as refused:
        await _rollback(w, "S9", s, dry_run=False)
    assert refused.value.status == "completed"


async def test_a_later_write_by_someone_else_skips_the_memory(world: World) -> None:
    w = world
    target = await w.create(w.member, None, summary="original")
    await w.update(target, w.owner, "S5", summary="by S5")
    await w.update(target, w.member, "other", importance=0.1)
    gone = await w.create(w.owner, "S5")
    await w.session.commit()
    before = await w.row(target)

    result = await _rollback(w, "S5", SessionPrincipal(w.owner, None), dry_run=False)
    await w.session.commit()
    assert result.summary["skipped_items"] == [
        {"memory_id": str(target), "kind": "updated", "cause": "modified_since"}
    ]
    assert await w.row(target) == before
    assert (await w.row(gone))["deleted_at"] is not None


async def test_a_metadata_restore_reaches_the_payload_only_after_the_commit(
    world: World, _stubs: dict[str, Any]
) -> None:
    """v0.113.0 review F1: a call whose transaction is rolled back (a tool
    timeout, a failed commit) leaves the vector payload untouched; the
    committed call patches it in ``finish``."""
    w = world
    target = await w.create(w.member, None, tags=["orig"], importance=0.5)
    await w.update(target, w.owner, "P1", tags=["session"], importance=0.9)
    await w.session.commit()
    s = SessionPrincipal(w.owner, None)

    lost = await _rollback(w, "P1", s, dry_run=False)
    assert [sync.memory_id for sync in lost.payload_syncs] == [target]
    await w.session.rollback()  # the commit never happens
    _stubs["payload"].assert_not_awaited()
    row = await w.row(target)
    assert (row["tags"], row["importance"], row["version"]) == (["session"], 0.9, 2)

    done = await _rollback(w, "P1", s, dry_run=False)
    await w.session.commit()
    _stubs["payload"].assert_not_awaited()
    assert (await w.row(target))["tags"] == ["orig"]
    # Someone edits the tags after the rollback committed, before finish():
    # the payload gets the row as it is now, not the restored values.
    await w.update(target, w.member, "later", tags=["later"])
    await w.session.commit()
    await sr.finish(done, w.session)
    _stubs["payload"].assert_awaited_once()
    kwargs = _stubs["payload"].await_args.kwargs
    assert kwargs["memory_id"] == target
    assert kwargs["payload_updates"]["tags"] == ["later"]
    assert kwargs["payload_updates"]["importance"] == 0.5
    # A memory forgotten before finish() runs gets no payload patch.
    _stubs["payload"].reset_mock()
    await w.forget(target, w.member, "later")
    await w.session.commit()
    await sr.finish(done, w.session)
    _stubs["payload"].assert_not_awaited()


async def test_a_spent_time_budget_spreads_the_batch_over_calls(
    world: World, _stubs: dict[str, Any]
) -> None:
    """v0.113.0 review F2: each call starts items only within its budget and
    commits what it finished; the next call continues."""
    w = world
    forgotten = [await w.create(w.member, None) for _ in range(3)]
    for memory_id in forgotten:
        await w.forget(memory_id, w.owner, "SLOW")
    await w.session.commit()
    s = SessionPrincipal(w.owner, None)

    first = await _rollback(w, "SLOW", s, dry_run=False, time_budget=0)
    await w.session.commit()
    assert (first.status, first.events, first.remaining) == ("in_progress", 1, 2)
    assert first.summary["restored"] == 1 and first.errors == []
    calls = [first]
    while calls[-1].remaining:
        calls.append(await _rollback(w, "SLOW", s, dry_run=False, time_budget=0))
        await w.session.commit()
        assert len(calls) <= 3
    assert calls[-1].status == "completed" and calls[-1].totals["restored"] == 3
    for memory_id in forgotten:
        assert (await w.row(memory_id))["deleted_at"] is None


async def test_the_sessions_own_write_during_its_rollback_is_rolled_back_too(
    world: World, async_engine: AsyncEngine
) -> None:
    """v0.113.0 review F7: the session's own edit holds the row lock when the
    rollback reads its batch. Once the lock is granted the edit's event is
    part of the plan — the memory goes back to its pre-session state instead
    of being skipped as ``modified_since`` with the earlier edit marked
    handled for good."""
    w = world
    target = await w.create(w.member, None, tags=["orig"])
    await w.update(target, w.owner, "S7", tags=["first"])
    await w.session.commit()
    s = SessionPrincipal(w.owner, None)
    rollback_pid = await w.session.scalar(text("SELECT pg_backend_pid()"))
    maker = _maker(async_engine)

    async with maker() as writer:
        await writer.execute(
            text(
                "UPDATE memories SET tags = ARRAY['second'], version = 3, session_id = 'S7' "
                "WHERE id = :m"
            ),
            {"m": target},
        )
        writer.add(
            MemoryRevision(
                memory_id=target,
                context_id=w.context_id,
                version=3,
                kind="updated",
                writer="api",
                actor_user_id=w.owner,
                session_id="S7",
                prior={"tags": ["first"]},
                at=utcnow(),
            )
        )
        await writer.flush()
        task = asyncio.create_task(_rollback(w, "S7", s, dry_run=False))
        for _ in range(200):  # until the rollback waits on the row lock
            async with maker() as probe:
                waiting = await probe.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :p AND NOT granted)"),
                    {"p": rollback_pid},
                )
            if waiting or task.done():
                break
            await asyncio.sleep(0.05)
        assert waiting, "the rollback never waited on the row lock"
        await writer.commit()
    result = await task
    await w.session.commit()

    assert result.summary["skipped"] == 0 and result.summary["updated_restored"] == 1
    assert (result.status, result.remaining) == ("completed", 0)
    row = await w.row(target)
    assert (row["tags"], row["version"]) == (["orig"], 4)
    handled = await w.session.scalar(
        select(func.count()).where(
            MemoryRevision.session_id == "S7",
            MemoryRevision.writer == "api",
            MemoryRevision.undone_by == result.rollback_id,
        )
    )
    assert handled == 2


async def test_mcp_handler_uses_the_credential_principal(world: World) -> None:
    from mcp_server.tools import session_rollback as tool

    ids = await _interleaved(world)

    async def fake_get_db() -> AsyncIterator[AsyncSession]:
        yield world.session

    async def call(user_id: str, session_id: str, agent: uuid.UUID | None) -> dict[str, Any]:
        set_agent_scope(AgentScope(agent_id=agent, enforcement_mode="enforce") if agent else None)
        try:
            with (
                patch("db.base.get_db", fake_get_db),
                patch.object(
                    tool,
                    "_resolve_context",
                    AsyncMock(
                        return_value=MagicMock(
                            id=world.context_id,
                            workspace_id=world.workspace_id,
                        )
                    ),
                ),
                patch.object(tool, "_context_response_fields", lambda _c: {}),
                patch.object(tool, "_log_tool_usage", AsyncMock()),
                patch(
                    "services.session_rollback.context_role",
                    AsyncMock(return_value=ContextRole.EDITOR),
                ),
            ):
                out = await tool.handle_rollback_session(
                    {"context_id": str(world.context_id), "session_id": session_id},
                    user_id,
                    world.workspace_id,
                )
        finally:
            set_agent_scope(None)
        return json.loads(out[0].text)

    # The member naming the owner's S2-free session id S1 sees only their own.
    mine = await call(world.member, "S1", None)
    assert mine["status"] == "dry_run"
    assert [i["memory_id"] for i in mine["items"]] == [str(ids["m3"])]
    # The member naming S2 is fine (theirs); the owner naming S2 is not.
    other = await call(world.owner, "S2", None)
    assert other == {
        "status": "error",
        "error": "session_not_found",
        "message": other["message"],
        "session_id": "S2",
        "help": other["help"],
    }
    # The owner's agent credential cannot reach the owner's unbound S1.
    agent = await call(world.owner, "S1", world.agent)
    assert agent["error"] == "session_not_found"


async def test_erasure_scrubs_session_tokens(world: World) -> None:
    from services.account_erasure_service import AccountErasureService

    w = world
    mine = await w.create(w.member, "M-SESSION")
    edited_later = await w.create(w.member, "M-SESSION")
    await w.update(edited_later, w.owner, "O-SESSION", summary="owner edit")
    w.session.add_all(
        [
            MemorySessionRollback(
                workspace_id=w.workspace_id,
                context_id=w.context_id,
                session_id="M-SESSION",
                principal_user_id=w.member,
                requested_by=w.member,
                status="completed",
            ),
            MemorySessionRollback(
                workspace_id=w.workspace_id,
                context_id=w.context_id,
                session_id="O-SESSION",
                principal_user_id=None,
                requested_by=w.member,
                status="completed",
            ),
            # #2121: a window-mode row of the subject.
            MemorySessionRollback(
                workspace_id=w.workspace_id,
                context_id=w.context_id,
                session_id=None,
                principal_user_id=w.member,
                requested_by=w.member,
                status="completed",
                window_since=w.clock[0],
                window_until=w.clock[-1],
            ),
        ]
    )
    await w.session.commit()

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = w.session
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        counts = await service._erase_session_provenance(w.member)
        pseudonym = service._pseudonym(w.member)
    await w.session.commit()

    assert counts == {"memory_sessions_scrubbed": 1, "session_rollbacks_pseudonymized": 5}
    assert (await w.row(mine))["session_id"] is None
    # The owner's later session on the member's memory is not the member's.
    assert (await w.row(edited_later))["session_id"] == "O-SESSION"
    rows = (
        await w.session.execute(
            select(
                MemorySessionRollback.session_id,
                MemorySessionRollback.principal_user_id,
                MemorySessionRollback.requested_by,
                MemorySessionRollback.window_since.is_not(None),
            )
            .where(MemorySessionRollback.context_id == w.context_id)
            .order_by(MemorySessionRollback.id)
        )
    ).all()
    # The owner-scope row the subject requested may name the subject's own
    # session: its token goes too.
    assert [tuple(r) for r in rows] == [
        (None, pseudonym, pseudonym, False),
        (None, None, pseudonym, False),
        # The window bounds are not personal data and stay.
        (None, pseudonym, pseudonym, True),
    ]


def test_f04_round_trip() -> None:
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    f04, prior = "f04_2044_session_provenance", "f03_2043_memory_versions"
    user = f"f04-migration-{uuid.uuid4().hex[:8]}"
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to(prior)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memories (id, user_id, workspace_id, summary, content, type, "
                    "embedding_status, importance, confidence, scope, long_term, access_count, "
                    "client, source, created_at) VALUES (gen_random_uuid(), :u, "
                    "gen_random_uuid(), 'seed', 'body', 'note', 'success', 0.5, 1.0, 'working', "
                    "false, 0, 'test', 'mcp_remember', now())"
                ),
                {"u": user},
            )

        _upgrade_to(f04)
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT agent_id, session_id FROM memories WHERE user_id = :u"), {"u": user}
            ).one()
            assert tuple(row) == (None, None)  # old rows stay NULL
            valid = conn.execute(
                text(
                    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i "
                    "ON c.oid = i.indexrelid WHERE c.relname = 'idx_memories_context_session'"
                )
            ).scalar_one()
            assert valid is True
            columns = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memory_revisions'"
                    )
                )
            }
            assert "undone_by" in columns
            check = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'valid_session_rollback_status'"
                )
            ).scalar_one()
            for status in ("in_progress", "completed", "partial"):
                assert f"'{status}'" in check

        _downgrade_to(prior)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('memory_session_rollbacks')")).scalar()
                is None
            )
            left = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "(table_name = 'memories' AND column_name IN ('agent_id', 'session_id')) "
                    "OR (table_name = 'memory_revisions' AND column_name = 'undone_by')"
                )
            ).all()
            assert left == []
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": user})
            finally:
                engine.dispose()


# ---------------------------------------------------------------------------
# #2121: the window mode
# ---------------------------------------------------------------------------

_MS = timedelta(milliseconds=1)


def _mark(world: World) -> Any:
    """A window bound between the last write and the next (ticks are 5 ms apart)."""
    return world.clock[-1] + _MS


async def _window(
    world: World,
    principal: SessionPrincipal,
    since: Any,
    until: Any,
    *,
    dry_run: bool,
    session: AsyncSession | None = None,
) -> sr.RollbackResult:
    return await sr.rollback_session(
        session or world.session,
        scope=sr.SessionScope(world.context_id, None, principal, since=since, until=until),
        workspace_id=world.workspace_id,
        requested_by=principal.actor_user_id,
        dry_run=dry_run,
        classify=_errors,
        new_correlation_id=_cid,
    )


async def _rollback_events(world: World, memory_id: uuid.UUID) -> list[str]:
    return list(
        (
            await world.session.execute(
                select(MemoryRevision.kind)
                .where(MemoryRevision.memory_id == memory_id, MemoryRevision.writer == "rollback")
                .order_by(MemoryRevision.id)
            )
        ).scalars()
    )


async def test_window_rolls_back_only_the_principals_in_window_writes(world: World) -> None:
    w, member, owner = world, world.member, world.owner
    me = SessionPrincipal(member, None)
    before = await w.create(member, None, summary="mine, before")
    shared = await w.create(owner, None, summary="owner's", tags=["o"])
    doomed = await w.create(member, None, summary="mine, forgotten in the window")
    since = _mark(w)
    created = await w.create(member, None)  # in the window, no session
    await w.update(shared, member, "stateful-1", tags=["m"])  # a session id too
    await w.update(before, member, None, summary="mine, edited in the window")
    owners = await w.create(owner, None)  # another user
    agents = await w.create(member, None, agent=w.agent)  # same user, agent key
    await w.forget(doomed, member, None)
    later_edit = await w.create(member, None)
    until = _mark(w)
    after = await w.create(member, None)  # after the window
    await w.update(later_edit, member, None, importance=0.1)  # own write after until
    await w.session.commit()
    untouched = {m: await w.row(m) for m in (owners, agents, after)}

    plan = await _window(w, me, since, until, dry_run=True)
    by_memory = {item.memory_id: item for item in plan.items}
    assert set(by_memory) == {created, shared, before, doomed, later_edit}
    assert by_memory[created].action == sr.ACTION_FORGET
    assert by_memory[shared].action == sr.ACTION_RESTORE
    assert by_memory[before].values == {"summary": "mine, before"}
    assert by_memory[doomed].action == sr.ACTION_UNFORGET
    # The principal's own write after until changed the memory since.
    assert (by_memory[later_edit].action, by_memory[later_edit].cause) == (
        sr.ACTION_SKIP,
        sr.CAUSE_MODIFIED_SINCE,
    )
    assert (plan.events, plan.remaining) == (5, 0)
    records = select(func.count()).where(MemorySessionRollback.context_id == w.context_id)
    assert await w.session.scalar(records) == 0  # a dry run writes nothing

    result = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert (result.status, result.remaining, result.errors) == ("completed", 0, [])
    assert (await w.row(created))["deleted_at"] is not None
    assert (await w.row(shared))["tags"] == ["o"]
    assert (await w.row(before))["summary"] == "mine, before"
    assert (await w.row(doomed))["deleted_at"] is None
    assert (await w.row(later_edit))["deleted_at"] is None
    for memory_id, row in untouched.items():
        assert await w.row(memory_id) == row
    record = await w.session.get(MemorySessionRollback, result.rollback_id)
    assert record is not None
    assert (record.session_id, record.principal_user_id, record.agent_id) == (None, member, None)
    assert (record.window_since, record.window_until) == (since, until)


async def test_window_never_selects_another_agent_or_the_unbound_user(world: World) -> None:
    w, owner = world, world.owner
    other_agent = uuid.uuid4()
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    unbound = await w.create(owner, None)
    bound = await w.create(owner, None, agent=w.agent)
    unbound_2 = await w.create(owner, "S-1")
    until = _mark(w)
    await w.session.commit()

    user_plan = await _window(w, SessionPrincipal(owner, None), since, until, dry_run=True)
    agent_plan = await _window(w, SessionPrincipal(owner, w.agent), since, until, dry_run=True)
    assert {i.memory_id for i in user_plan.items} == {unbound, unbound_2}
    assert {i.memory_id for i in agent_plan.items} == {bound}
    with pytest.raises(sr.NoWritesInWindowError):
        await _window(w, SessionPrincipal(owner, other_agent), since, until, dry_run=True)
    with pytest.raises(sr.NoWritesInWindowError):
        await _window(w, SessionPrincipal(w.member, None), since, until, dry_run=True)


async def test_two_clients_one_credential_are_both_in_the_window(world: World) -> None:
    """A user credential running two clients: one window covers both (documented)."""
    w, member = world, world.member
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    stateless = await w.create(member, None)
    stateful = await w.create(member, "mcp-session-b")
    stateless_2 = await w.create(member, None)
    until = _mark(w)
    await w.session.commit()
    me = SessionPrincipal(member, None)
    plan = await _window(w, me, since, until, dry_run=True)
    assert {i.memory_id for i in plan.items} == {stateless, stateful, stateless_2}
    page = await changes_in_session(
        w.session,
        context_id=w.context_id,
        session_id=None,
        principal=me,
        owner_user_id=None,
        since=since,
        until=until,
    )
    assert [c.memory_id for c in page.changes] == [stateless, stateful, stateless_2]
    result = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert result.summary["forgotten"] == 3


async def test_window_continues_under_the_same_record(world: World) -> None:
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    created = [await w.create(member, None) for _ in range(5)]
    until = _mark(w)
    await w.session.commit()
    assert await sr.unfinished_window_until(w.session, w.context_id, me, since) is None
    with patch.object(sr, "MAX_ROLLBACK_EVENTS_PER_CALL", 2):
        first = await _window(w, me, since, until, dry_run=False)
        await w.session.commit()
        assert (first.status, first.events, first.remaining) == ("in_progress", 2, 3)
        # until omitted on the follow-up: the unfinished record's until.
        reused = await sr.unfinished_window_until(w.session, w.context_id, me, since)
        assert reused == until
        # Another principal (the agent key) has no unfinished window here.
        agent = SessionPrincipal(member, w.agent)
        assert await sr.unfinished_window_until(w.session, w.context_id, agent, since) is None
        second = await _window(w, me, since, reused, dry_run=False)
        await w.session.commit()
        third = await _window(w, me, since, until, dry_run=False)
        await w.session.commit()
    assert (third.status, third.remaining) == ("completed", 0)
    assert first.rollback_id == second.rollback_id == third.rollback_id
    assert third.totals["forgotten"] == 5 and third.totals["calls"] == 3
    assert await sr.unfinished_window_until(w.session, w.context_id, me, since) is None
    for dry_run in (True, False):
        with pytest.raises(sr.AlreadyRolledBackError) as refused:
            await _window(w, me, since, until, dry_run=dry_run)
        assert refused.value.rollback_id == first.rollback_id
        await w.session.rollback()
    for memory_id in created:
        assert (await w.row(memory_id))["deleted_at"] is not None
    # A write that was in flight lands inside the window afterwards: it
    # reopens the same record.
    late = await w.create(member, None)
    await w.session.execute(
        text("UPDATE memory_revisions SET at = :at WHERE memory_id = :m"),
        {"at": until - _MS, "m": late},
    )
    await w.session.commit()
    again = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert (again.rollback_id, again.status) == (first.rollback_id, "completed")
    assert [i.memory_id for i in again.items] == [late]


async def test_a_wider_window_reverses_nothing_twice(world: World) -> None:
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    first = await w.create(member, None)
    narrow_until = _mark(w)
    second = await w.create(member, None)
    wide_until = _mark(w)
    await w.session.commit()

    narrow = await _window(w, me, since, narrow_until, dry_run=False)
    await w.session.commit()
    assert [i.memory_id for i in narrow.items] == [first]
    wide = await _window(w, me, since, wide_until, dry_run=False)
    await w.session.commit()
    assert [i.memory_id for i in wide.items] == [second]
    assert wide.rollback_id != narrow.rollback_id
    assert await _rollback_events(w, first) == ["forgotten"]
    assert await _rollback_events(w, second) == ["forgotten"]
    # Each window answers with its own record from now on.
    for until, record in ((narrow_until, narrow), (wide_until, wide)):
        with pytest.raises(sr.AlreadyRolledBackError) as refused:
            await _window(w, me, since, until, dry_run=False)
        assert refused.value.rollback_id == record.rollback_id
        await w.session.rollback()
    # A third window whose events another window handled: refused, no record.
    with pytest.raises(sr.AlreadyRolledBackError) as refused:
        await _window(w, me, since + _MS, wide_until, dry_run=True)
    assert refused.value.rollback_id is None


async def test_session_then_window_reverses_nothing_twice(world: World) -> None:
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    target = await w.create(w.owner, None, summary="original")
    await w.update(target, member, "S-STATEFUL", summary="by the session")
    outside = await w.create(member, None)  # same principal, no session
    until = _mark(w)
    await w.session.commit()

    session = await _rollback(w, "S-STATEFUL", me, dry_run=False)
    await w.session.commit()
    assert session.summary["updated_restored"] == 1
    window = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert [i.memory_id for i in window.items] == [outside]
    assert await _rollback_events(w, target) == ["updated"]
    assert (await w.row(target))["summary"] == "original"


async def test_concurrent_session_and_window_rollbacks_reverse_once(
    world: World, async_engine: AsyncEngine
) -> None:
    """Different advisory locks; the memory row lock and the ``undone_by``
    re-read under it keep the window from reversing what the session's
    rollback (committed while the window waited) already reversed."""
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    target = await w.create(w.owner, None, tags=["orig"])
    await w.update(target, member, "S-RACE", tags=["session"])
    until = _mark(w)
    await w.session.commit()
    window_pid = await w.session.scalar(text("SELECT pg_backend_pid()"))
    maker = _maker(async_engine)

    async with maker() as other:
        session = await sr.rollback_session(
            other,
            scope=sr.SessionScope(w.context_id, "S-RACE", me),
            workspace_id=w.workspace_id,
            requested_by=member,
            dry_run=False,
            classify=_errors,
            new_correlation_id=_cid,
        )
        assert session.summary["updated_restored"] == 1
        task = asyncio.create_task(_window(w, me, since, until, dry_run=False))
        waiting = False
        for _ in range(200):  # until the window waits on the row lock
            async with maker() as probe:
                waiting = bool(
                    await probe.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :p AND NOT granted)"
                        ),
                        {"p": window_pid},
                    )
                )
            if waiting or task.done():
                break
            await asyncio.sleep(0.05)
        assert waiting, "the window rollback never waited on the row lock"
        await other.commit()
    with pytest.raises(sr.AlreadyRolledBackError) as refused:
        await task
    assert refused.value.rollback_id is None
    await w.session.rollback()
    assert await _rollback_events(w, target) == ["updated"]
    assert (await w.row(target))["tags"] == ["orig"]


async def test_window_without_writes_is_no_writes_in_window(world: World) -> None:
    w = world
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    await w.create(w.owner, None)  # another principal wrote in the window
    until = _mark(w)
    await w.session.commit()
    with pytest.raises(sr.NoWritesInWindowError):
        await _window(w, SessionPrincipal(w.member, None), since, until, dry_run=False)
    await w.session.rollback()
    # Rollback-written events alone are nothing to undo either. The rollback's
    # events are moved an hour back, away from the API write (the World clock
    # runs a few ms ahead of now, so "just after the rollback" could catch it).
    owner = SessionPrincipal(w.owner, None)
    await _window(w, owner, since, until, dry_run=False)
    await w.session.commit()
    parked = since - timedelta(hours=1)
    moved = await w.session.execute(
        text("UPDATE memory_revisions SET at = :at WHERE context_id = :c AND writer = 'rollback'"),
        {"at": parked, "c": w.context_id},
    )
    assert cast(Any, moved).rowcount > 0  # the rollback did write its own events
    await w.session.commit()
    with pytest.raises(sr.NoWritesInWindowError):
        await _window(w, owner, parked - _MS, parked + _MS, dry_run=True)


async def test_changes_in_session_by_principal_respects_the_private_owner_filter(
    world: World,
) -> None:
    w, member, owner = world, world.member, world.owner
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    mine = await w.create(member, None)
    theirs = await w.create(owner, None)
    await w.update(theirs, member, "S-X", summary="member edit of the owner's memory")
    await w.create(member, None, agent=w.agent)
    await w.create(owner, None)
    await w.session.commit()
    me = SessionPrincipal(member, None)

    async def page(**kw: Any) -> list[tuple[uuid.UUID, str]]:
        result = await changes_in_session(
            w.session,
            context_id=w.context_id,
            principal=me,
            since=since,
            until=None,
            **{"session_id": None, "owner_user_id": None, **kw},
        )
        return [(c.memory_id, c.kind) for c in result.changes]

    assert await page() == [(mine, "created"), (theirs, "updated")]
    # A private context lists only the memories the caller owns.
    assert await page(owner_user_id=member) == [(mine, "created")]
    # session_id narrows the principal's events to that session.
    assert await page(session_id="S-X") == [(theirs, "updated")]
    with pytest.raises(ValueError, match="principal"):
        await changes_in_session(
            w.session,
            context_id=w.context_id,
            session_id=None,
            principal=None,
            owner_user_id=None,
            since=since,
            until=None,
        )


async def test_mcp_handler_window_is_the_callers_own_even_for_an_owner(world: World) -> None:
    from mcp_server.tools import session_rollback as tool
    from utils.datetime import to_utc_iso

    w = world
    since = utcnow() - timedelta(seconds=5)
    member_write = await w.create(w.member, None)
    owner_write = await w.create(w.owner, None)
    await w.session.commit()
    await asyncio.sleep(0.2)  # the World clock runs a little ahead of now

    async def fake_get_db() -> AsyncIterator[AsyncSession]:
        yield w.session

    async def call(user_id: str, **args: Any) -> dict[str, Any]:
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
                AsyncMock(return_value=ContextRole.OWNER),
            ),
        ):
            out = await tool.handle_rollback_session(
                {"context_id": str(w.context_id), **args}, user_id, w.workspace_id
            )
        return json.loads(out[0].text)

    plan = await call(w.owner, since=to_utc_iso(since))
    assert plan["status"] == "dry_run"
    assert [i["memory_id"] for i in plan["items"]] == [str(owner_write)]
    assert plan["since"] == to_utc_iso(since) and "session_id" not in plan
    done = await call(w.owner, since=to_utc_iso(since), until=plan["until"], dry_run=False)
    assert done["status"] == "rolled_back" and done["rollback_status"] == "completed"
    assert (await w.row(member_write))["deleted_at"] is None
    assert (await w.row(owner_write))["deleted_at"] is not None
    again = await call(w.owner, since=to_utc_iso(since), until=plan["until"], dry_run=False)
    assert again["error"] == "already_rolled_back"
    assert again["rollback_id"] == done["rollback_id"]
    empty = await call(
        w.owner,
        since=to_utc_iso(since - timedelta(days=1)),
        until=to_utc_iso(since - timedelta(hours=1)),
    )
    assert empty["error"] == "no_writes_in_window"


def test_f05_round_trip() -> None:
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    f05, prior = "f05_2121_rollback_window", "f04_2044_session_provenance"
    marker = f"f05-migration-{uuid.uuid4().hex[:8]}"
    ws_id, ctx_id = uuid.uuid4(), uuid.uuid4()
    _reset_alembic_state()
    engine = _sync_engine()

    def columns(conn: Any) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'memory_session_rollbacks'"
                )
            )
        }

    def index_valid(conn: Any, name: str) -> Any:
        return conn.execute(
            text(
                "SELECT i.indisvalid FROM pg_class c JOIN pg_index i "
                "ON c.oid = i.indexrelid WHERE c.relname = :n"
            ),
            {"n": name},
        ).scalar()

    try:
        _upgrade_to(prior)
        with engine.connect() as conn:
            assert "window_since" not in columns(conn)
        _upgrade_to(f05)
        with engine.connect() as conn:
            assert {"window_since", "window_until"} <= columns(conn)
            assert index_valid(conn, "idx_memory_revisions_actor_at") is True
            assert index_valid(conn, "uq_memory_session_rollbacks_window") is True
            definition = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": "uq_memory_session_rollbacks_window"},
            ).scalar_one()
            assert "UNIQUE" in definition and "WHERE (window_since IS NOT NULL)" in definition
        with engine.begin() as conn:
            # Through the model tables, so their Python-side defaults apply.
            conn.execute(
                Workspace.__table__.insert().values(id=ws_id, name=marker, owner_user_id=marker)
            )
            conn.execute(
                Context.__table__.insert().values(
                    id=ctx_id, workspace_id=ws_id, name="f05", created_by=marker
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_session_rollbacks (workspace_id, context_id, "
                    "principal_user_id, requested_by, status, window_since, window_until) "
                    "VALUES (:w, :c, :u, :u, 'completed', now() - interval '2 hours', "
                    "now() - interval '1 hour')"
                ),
                {"w": ws_id, "c": ctx_id, "u": marker},
            )
        _downgrade_to(prior)
        with engine.connect() as conn:
            assert not ({"window_since", "window_until"} & columns(conn))
            left = conn.execute(
                text("SELECT count(*) FROM memory_session_rollbacks WHERE requested_by = :u"),
                {"u": marker},
            ).scalar_one()
            assert left == 0  # window rows do not survive as erased-looking session rows
            assert index_valid(conn, "idx_memory_revisions_actor_at") is None
            assert index_valid(conn, "uq_memory_session_rollbacks_window") is None
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws_id})
            finally:
                engine.dispose()


async def test_window_skips_a_memory_another_writer_changed_after_it(world: World) -> None:
    """Other writers' edits INSIDE the window (another user, the same user's
    agent key, and for an agent principal the same user's unbound credential)
    are not the principal's: each memory is skipped, never reverted over them,
    while the principal's own last write is restored.

    Two guards decide it — the foreign predicate's actor / agent terms and the
    planner's version check (an edit moves the version) — and either alone
    skips these memories, so this pins the behavior, not one guard. The
    predicate's terms themselves are pinned by the compile tests in
    tests/services/test_session_rollback_planner.py."""
    w, member, owner = world, world.member, world.owner
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    by_owner = await w.create(member, None, summary="edited by the owner")
    by_agent = await w.create(member, None, summary="edited by my agent key")
    agent_made = await w.create(member, None, agent=w.agent, summary="my agent's")
    await w.update(by_owner, owner, None, importance=0.2)  # another actor
    await w.update(by_agent, member, None, agent=w.agent, importance=0.3)  # another agent
    await w.update(agent_made, member, None, importance=0.4)  # unbound vs the agent
    until = _mark(w)
    await w.session.commit()
    before = {m: await w.row(m) for m in (by_owner, by_agent, agent_made)}

    plan = await _window(w, me, since, until, dry_run=True)
    causes = {i.memory_id: (i.action, i.cause) for i in plan.items}
    assert causes == {
        by_owner: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),  # actor term
        by_agent: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),  # agent IS DISTINCT FROM NULL
        # The unbound edit is the user's own; the agent's earlier create blocks nothing.
        agent_made: (sr.ACTION_RESTORE, None),
    }
    agent_plan = await _window(w, SessionPrincipal(member, w.agent), since, until, dry_run=True)
    agent_causes = {i.memory_id: (i.action, i.cause) for i in agent_plan.items}
    assert agent_causes == {
        # The user's unbound edit after the agent's create: IS DISTINCT FROM '<agent>'.
        agent_made: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),
        # The agent's own edit is the last write there.
        by_agent: (sr.ACTION_RESTORE, None),
    }
    await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    for memory_id in (by_owner, by_agent):
        assert await w.row(memory_id) == before[memory_id]
    assert (await w.row(agent_made))["importance"] == 0.5


async def test_a_foreign_write_between_the_principals_own_is_never_reverted_over(
    world: World,
) -> None:
    """Only the foreign predicate's actor / agent terms guard this: another
    writer's edit sits between two of the principal's own writes inside the
    window, so the version matches the principal's last write and no
    tombstone is involved. Without the terms the plan would forget each
    memory and the other writer's edit with it."""
    w, member, owner = world, world.member, world.owner
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    by_owner = await w.create(member, None)
    await w.update(by_owner, owner, None, importance=0.2)  # another actor
    await w.update(by_owner, member, None, tags=["mine"])
    by_agent = await w.create(member, None)
    await w.update(by_agent, member, None, agent=w.agent, importance=0.3)  # my agent key
    await w.update(by_agent, member, None, tags=["mine"])
    agent_made = await w.create(member, None, agent=w.agent)
    await w.update(agent_made, member, None, importance=0.4)  # the unbound user
    await w.update(agent_made, member, None, agent=w.agent, tags=["agent"])
    until = _mark(w)
    await w.session.commit()

    user_plan = await _window(w, SessionPrincipal(member, None), since, until, dry_run=True)
    assert {i.memory_id: (i.action, i.cause) for i in user_plan.items} == {
        by_owner: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),
        by_agent: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),
        # The unbound user's edit sits between the agent's writes: its own
        # (only) event is not the last one, so the version check skips it.
        agent_made: (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE),
    }
    agent_plan = await _window(w, SessionPrincipal(member, w.agent), since, until, dry_run=True)
    agent_causes = {i.memory_id: (i.action, i.cause) for i in agent_plan.items}
    assert agent_causes[agent_made] == (sr.ACTION_SKIP, sr.CAUSE_MODIFIED_SINCE)


async def test_unfinished_window_until_prefers_the_newest_partial_or_in_progress(
    world: World,
) -> None:
    w = world
    me = SessionPrincipal(w.member, None)
    since = utcnow() - timedelta(hours=3)

    def record(until: Any, status: str) -> MemorySessionRollback:
        return MemorySessionRollback(
            workspace_id=w.workspace_id,
            context_id=w.context_id,
            principal_user_id=w.member,
            requested_by=w.member,
            status=status,
            window_since=since,
            window_until=until,
        )

    first, second, done = (since + timedelta(hours=h) for h in (1, 2, 3))
    w.session.add(record(first, "in_progress"))
    await w.session.flush()
    w.session.add(record(second, "partial"))
    await w.session.flush()
    w.session.add(record(done, "completed"))
    await w.session.commit()
    assert await sr.unfinished_window_until(w.session, w.context_id, me, since) == second
    # Another since, or the agent key, has none.
    assert await sr.unfinished_window_until(w.session, w.context_id, me, first) is None
    agent = SessionPrincipal(w.member, w.agent)
    assert await sr.unfinished_window_until(w.session, w.context_id, agent, since) is None


async def test_the_window_key_is_unique_per_principal(world: World) -> None:
    """The partial unique index, NULL agent included (COALESCE)."""
    from sqlalchemy.exc import IntegrityError

    w = world
    since = utcnow() - timedelta(hours=2)
    until = since + timedelta(hours=1)

    def record(agent: Any) -> MemorySessionRollback:
        return MemorySessionRollback(
            workspace_id=w.workspace_id,
            context_id=w.context_id,
            principal_user_id=w.member,
            agent_id=agent,
            requested_by=w.member,
            status="completed",
            window_since=since,
            window_until=until,
        )

    w.session.add_all([record(None), record(w.agent)])
    await w.session.commit()
    w.session.add(record(None))
    with pytest.raises(IntegrityError):
        await w.session.commit()
    await w.session.rollback()


async def test_window_then_session_reverses_nothing_twice(world: World) -> None:
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    since = (w.clock[-1] if w.clock else utcnow()) - timedelta(seconds=1)
    target = await w.create(member, "S-AFTER")
    until = _mark(w)
    await w.session.commit()
    window = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert [i.memory_id for i in window.items] == [target]
    with pytest.raises(sr.AlreadyRolledBackError) as refused:
        await _rollback(w, "S-AFTER", me, dry_run=False)
    assert refused.value.rollback_id is None
    await w.session.rollback()
    assert await _rollback_events(w, target) == ["forgotten"]


async def test_a_window_record_whose_writes_are_gone_is_closed(world: World) -> None:
    """Its memories purged (the revisions cascade away), an unfinished window
    is closed by the next call instead of pinning since-only calls to its
    old until with no_writes_in_window forever."""
    w = world
    me = SessionPrincipal(w.member, None)
    since = utcnow() - timedelta(hours=2)
    until = since + timedelta(hours=1)
    w.session.add(
        MemorySessionRollback(
            workspace_id=w.workspace_id,
            context_id=w.context_id,
            principal_user_id=w.member,
            requested_by=w.member,
            status="in_progress",
            summary={"forgotten": 3, "calls": 1},
            window_since=since,
            window_until=until,
        )
    )
    await w.session.commit()
    assert await sr.unfinished_window_until(w.session, w.context_id, me, since) == until
    plan = await _window(w, me, since, until, dry_run=True)
    assert (plan.items, plan.rollback_id is not None) == ([], True)
    closed = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert (closed.status, closed.remaining, closed.events) == ("completed", 0, 0)
    assert closed.totals["calls"] == 2
    assert await sr.unfinished_window_until(w.session, w.context_id, me, since) is None
    with pytest.raises(sr.AlreadyRolledBackError):
        await _window(w, me, since, until, dry_run=True)
    # A window that never had a record still answers no_writes_in_window.
    with pytest.raises(sr.NoWritesInWindowError):
        await _window(w, me, since - timedelta(hours=1), since, dry_run=True)


async def test_a_restore_stamps_its_events_and_the_row_from_one_server_clock(
    world: World,
) -> None:
    """#2135: an un-forget that also puts back values writes a ``restored`` and
    an ``updated`` event; both carry the one ``utcnow()`` the restore took, and
    so does the row's ``updated_at`` — never the database's ``now()``."""
    w, member = world, world.member
    me = SessionPrincipal(member, None)
    target = await w.create(member, None, summary="the value before the window")
    since = _mark(w)
    await w.update(target, member, None, summary="edited in the window")
    await w.forget(target, member, None)
    until = _mark(w)
    await w.session.commit()

    plan = await _window(w, me, since, until, dry_run=True)
    (item,) = plan.items
    assert (item.memory_id, item.action, item.unforget) == (target, sr.ACTION_RESTORE, True)

    stamp = (utcnow() - timedelta(hours=1)).replace(microsecond=123456)
    with patch("services.session_rollback.utcnow", return_value=stamp):
        result = await _window(w, me, since, until, dry_run=False)
    await w.session.commit()
    assert (result.status, result.errors) == ("completed", [])

    rows = (
        await w.session.execute(
            select(MemoryRevision.kind, MemoryRevision.at)
            .where(MemoryRevision.memory_id == target, MemoryRevision.writer == "rollback")
            .order_by(MemoryRevision.id)
        )
    ).all()
    assert [tuple(r) for r in rows] == [("restored", stamp), ("updated", stamp)]
    row = await w.row(target)
    assert (row["deleted_at"], row["updated_at"]) == (None, stamp)
    assert row["summary"] == "the value before the window"


async def test_a_restored_details_edit_brings_back_its_observed_at(
    world: World, _stubs: dict[str, Any]
) -> None:
    """#2330: the generated column follows the restored details, and the
    payload patch sets the restored event time."""
    w = world
    target = await w.create(w.member, None, details={"observed_at": "2024-03-07"})
    await w.update(target, w.owner, "OBS", details={"observed_at": "2025-01-01T00:00:00Z"})
    await w.session.commit()

    done = await _rollback(w, "OBS", SessionPrincipal(w.owner, None), dry_run=False)
    await w.session.commit()
    observed = (
        await w.session.execute(
            text("SELECT observed_at FROM memories WHERE id = :m"), {"m": target}
        )
    ).scalar_one()
    assert observed == "2024-03-07T00:00:00Z"

    await sr.finish(done, w.session)
    kwargs = _stubs["payload"].await_args.kwargs
    assert kwargs["payload_updates"]["observed_at"] == "2024-03-07T00:00:00Z"
    assert "observed_at" not in (kwargs.get("delete_keys") or [])


async def test_a_session_over_an_erased_members_keys_rolls_back_without_them(
    world: World, _stubs: dict[str, Any]
) -> None:
    """#2275: an erased member added ``s``; the owner's session rewrote both
    keys. The erasure reverts nothing live (the session's ``s`` is the
    owner's) but removes the member's ``s`` from the session's prior — which
    keeps ``a``. Rolling the session back then puts the owner's pre-session
    value back without the member's key (before #2275 the whole prior was
    gone and the rollback skipped the memory)."""
    from services.erasure_details import erase_subject_details

    w = world
    erased = f"erased-{uuid.uuid4().hex[:8]}"
    target = await w.create(w.member, None, details={"a": 1})
    await w.update(target, erased, None, details={"a": 1, "s": 1})
    await w.update(target, w.owner, "RB", details={"a": 2, "s": 2})
    await w.session.commit()

    result = await erase_subject_details(
        w.session, erased, actor="e" * 64, erased_actors=frozenset()
    )
    await w.session.commit()
    assert (result.restore.restored, result.strip.details_stripped) == (0, 1)
    prior = (
        await w.session.execute(
            select(MemoryRevision.prior).where(
                MemoryRevision.memory_id == target, MemoryRevision.session_id == "RB"
            )
        )
    ).scalar_one()
    assert prior == {"details": {"a": 1}}

    done = await _rollback(w, "RB", SessionPrincipal(w.owner, None), dry_run=False)
    await w.session.commit()
    assert done.summary["skipped_items"] == []
    assert (await w.row(target))["details"] == {"a": 1}
