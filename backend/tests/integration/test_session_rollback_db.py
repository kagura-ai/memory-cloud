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

The vector store, the embedding service and the permission check are stubbed.
Every seeded row goes with its workspace. Not executed in the local unit run —
needs the DB container.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
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
        patch(
            "services.memory_service.MemoryService._update_sync_qdrant_payload", AsyncMock()
        ) as payload,
    ):
        yield {"points": points, "reembed": reembed, "payload": payload}


def _errors(exc: BaseException) -> str:
    return "internal_error"


def _cid() -> str:
    return "corr-test"


async def _rollback(
    world: World, session_id: str, principal: SessionPrincipal | None, *, dry_run: bool
) -> sr.RollbackResult:
    return await sr.rollback_session(
        world.session,
        scope=sr.SessionScope(world.context_id, session_id, principal),
        workspace_id=world.workspace_id,
        requested_by=principal.actor_user_id if principal else world.owner,
        dry_run=dry_run,
        classify=_errors,
        new_correlation_id=_cid,
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
    assert sr.addressable_principal(world.owner, ContextRole.OWNER) is None
    assert sr.addressable_principal(world.member, ContextRole.EDITOR) == SessionPrincipal(
        world.member, None
    )
    set_agent_scope(AgentScope(agent_id=world.agent, enforcement_mode="enforce"))
    try:
        assert sr.addressable_principal(world.owner, ContextRole.OWNER) == SessionPrincipal(
            world.owner, world.agent
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
                patch.object(tool, "_check_viewer_permission", AsyncMock(return_value=None)),
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
        ]
    )
    await w.session.commit()

    service = AccountErasureService.__new__(AccountErasureService)
    service.db = w.session
    with patch("services.account_erasure_service._audit_salt", return_value="salt"):
        counts = await service._erase_session_provenance(w.member)
        pseudonym = service._pseudonym(w.member)
    await w.session.commit()

    assert counts == {"memory_sessions_scrubbed": 1, "session_rollbacks_pseudonymized": 3}
    assert (await w.row(mine))["session_id"] is None
    # The owner's later session on the member's memory is not the member's.
    assert (await w.row(edited_later))["session_id"] == "O-SESSION"
    rows = (
        await w.session.execute(
            select(
                MemorySessionRollback.session_id,
                MemorySessionRollback.principal_user_id,
                MemorySessionRollback.requested_by,
            )
            .where(MemorySessionRollback.context_id == w.context_id)
            .order_by(MemorySessionRollback.id)
        )
    ).all()
    # The owner-scope row the subject requested may name the subject's own
    # session: its token goes too.
    assert [tuple(r) for r in rows] == [
        (None, pseudonym, pseudonym),
        (None, None, pseudonym),
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
