"""End-to-end validation of the per-memory type/source binding filter (#1299)
against the REAL ``MemoryService`` + real test DB (no service mocking beyond
the Qdrant search backend) — the same flow shape as
``test_agent_features_e2e.py`` (#1295).

The binding here restricts the SAME bound context by row attributes
(``allowed_memory_types=['note']`` / ``allowed_source_types=['manual']``), so
these pins prove the filter is per-memory, not per-context: rows differing
only in ``type`` / ``source_type`` within one permitted context must diverge.
The service layer is the single enforcement site (REST, MCP, share-key and
bootstrap lanes all pass through it — #1291/#1292 parity by construction).

Setup rows are flushed (not committed) on ``db_session``; the only committed
side effect is the fail-open audit writer's independent session, cleaned up
by workspace_id in teardown (append-only trigger disabled transactionally).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from models.agent import Agent
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import (
    DELIVERY_MODE_ALWAYS,
    SOURCE_TYPE_API,
    SOURCE_TYPE_MANUAL,
    Memory,
    NeuralMemoryEdge,
)
from models.memory_access_event import MemoryAccessEvent
from models.schemas import ExploreRequest, ForgetRequest, RecallRequest
from services.agent_binding_service import ROW_FILTER_KIND, AgentBindingService
from services.memory_service import MemoryService
from utils.exceptions import NotFoundException


@pytest.fixture(autouse=True)
def _clean_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest_asyncio.fixture(loop_scope="session")
async def env(db_session):
    """Workspace + owner + ONE context + three agents (enforce-filtered,
    enforce-deny-all, shadow-filtered) + rows differing only in type/source.

    Creating the bindings WITH arrays through the service also pins the
    #1275 CRUD-rejection lift end to end.
    """
    uid = f"e2e-typef-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()

    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="E2E Type Filter Owner",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.flush()

    db_session.add(
        Workspace(
            id=ws_id,
            name=f"ws-{uuid.uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=uid,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
    )
    db_session.add(WorkspaceMember(workspace_id=ws_id, user_id=uid, role=WorkspaceRole.OWNER))
    await db_session.flush()

    ctx = Context(id=uuid.uuid4(), workspace_id=ws_id, name="bound", created_by=uid)
    db_session.add(ctx)
    await db_session.flush()

    agent_filtered = Agent(
        workspace_id=ws_id, name=f"filtered-{uuid.uuid4().hex[:6]}", owner_user_id=uid
    )
    agent_deny_all = Agent(
        workspace_id=ws_id, name=f"denyall-{uuid.uuid4().hex[:6]}", owner_user_id=uid
    )
    agent_shadow = Agent(
        workspace_id=ws_id,
        name=f"shadow-{uuid.uuid4().hex[:6]}",
        owner_user_id=uid,
        enforcement_mode="shadow",
    )
    db_session.add_all([agent_filtered, agent_deny_all, agent_shadow])
    await db_session.flush()

    svc = AgentBindingService(db_session)
    await svc.create_binding(
        agent=agent_filtered,
        context_id=ctx.id,
        created_by=uid,
        can_read=True,
        write_policy="direct",
        is_default=True,
        allowed_memory_types=["note"],
        allowed_source_types=["manual"],
    )
    await svc.create_binding(
        agent=agent_deny_all,
        context_id=ctx.id,
        created_by=uid,
        can_read=True,
        write_policy="direct",
        is_default=True,
        allowed_memory_types=[],
    )
    await svc.create_binding(
        agent=agent_shadow,
        context_id=ctx.id,
        created_by=uid,
        can_read=True,
        write_policy="direct",
        is_default=True,
        allowed_memory_types=["note"],
        allowed_source_types=["manual"],
    )

    def _mem(summary, *, mtype, source, delivery=None, details=None):
        return Memory(
            id=uuid.uuid4(),
            user_id=uid,
            workspace_id=ws_id,
            context_id=ctx.id,
            summary=summary,
            content=f"content of {summary}",
            type=mtype,
            client="test",
            tags=[],
            source_type=source,
            **({"delivery_mode": delivery} if delivery else {}),
            **({"details": details} if details else {}),
        )

    # trigger_from/until are STORED generated columns — seed the JSON only.
    mem_note = _mem(
        "note manual", mtype="note", source=SOURCE_TYPE_MANUAL, delivery=DELIVERY_MODE_ALWAYS
    )
    mem_time = _mem(
        "time manual",
        mtype="time",
        source=SOURCE_TYPE_MANUAL,
        delivery=DELIVERY_MODE_ALWAYS,
        details={"trigger": {"from": "2026-07-01T00:00:00", "until": "2027-01-01T00:00:00"}},
    )
    mem_api = _mem("note api", mtype="note", source=SOURCE_TYPE_API)
    db_session.add_all([mem_note, mem_time, mem_api])
    await db_session.flush()

    # Graph edge for the explore-neighbor pin: note -> time.
    db_session.add(
        NeuralMemoryEdge(
            user_id=uid,
            src_id=mem_note.id,
            dst_id=mem_time.id,
            workspace_id=ws_id,
            context_id=ctx.id,
            weight=0.5,
        )
    )
    await db_session.flush()

    yield {
        "uid": uid,
        "ws_id": ws_id,
        "ctx": ctx.id,
        "agent_filtered": agent_filtered.id,
        "agent_deny_all": agent_deny_all.id,
        "agent_shadow": agent_shadow.id,
        "mem_note": mem_note.id,
        "mem_time": mem_time.id,
        "mem_api": mem_api.id,
    }

    # Audit writer commits on an independent session — clean by workspace.
    # DISABLE + DELETE + ENABLE + COMMIT are one transaction (transactional
    # DDL): a failure rolls the DISABLE back too, so the append-only
    # invariant is never left off for later tests.
    await db_session.rollback()
    try:
        await db_session.execute(text("ALTER TABLE memory_access_events DISABLE TRIGGER USER"))
        await db_session.execute(
            delete(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == ws_id)
        )
        await db_session.execute(text("ALTER TABLE memory_access_events ENABLE TRIGGER USER"))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


def _svc(db_session, env):
    """Real MemoryService with ONLY the Qdrant search backend stubbed to
    return every seeded memory — the binding filter must do the narrowing."""
    svc = MemoryService(db_session)
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[
            {"id": str(env["mem_note"]), "score": 0.9},
            {"id": str(env["mem_time"]), "score": 0.8},
            {"id": str(env["mem_api"]), "score": 0.7},
        ]
    )
    return svc


def _scope(env, agent_key, mode="enforce"):
    set_agent_scope(
        AgentScope(agent_id=env[agent_key], enforcement_mode=mode, workspace_id=env["ws_id"])
    )


async def _recall_ids(svc, env):
    response = await svc.recall(
        RecallRequest(query="q", k=10),
        user_id=env["uid"],
        current_context_id=env["ctx"],
        current_workspace_id=env["ws_id"],
    )
    return {r.memory_id for r in response.results}


@pytest.mark.asyncio(loop_scope="session")
async def test_no_scope_recall_returns_all_rows(env, db_session):
    ids = await _recall_ids(_svc(db_session, env), env)
    assert ids == {env["mem_note"], env["mem_time"], env["mem_api"]}


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_recall_filters_by_type_and_source(env, db_session):
    _scope(env, "agent_filtered")
    ids = await _recall_ids(_svc(db_session, env), env)
    # time (type-denied) and api (source-denied) drop; note/manual survives.
    assert ids == {env["mem_note"]}

    rows = (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(
                    MemoryAccessEvent.workspace_id == env["ws_id"],
                    MemoryAccessEvent.operation == "recall",
                    MemoryAccessEvent.outcome == "success",
                )
            )
        )
        .scalars()
        .all()
    )
    counted = [r for r in rows if (r.event_metadata or {}).get("binding_row_filtered_count")]
    assert len(counted) == 1
    assert counted[0].event_metadata["binding_row_filtered_count"] == 2
    assert counted[0].event_metadata["filter_kind"] == ROW_FILTER_KIND
    assert counted[0].result_count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_empty_array_denies_every_row(env, db_session):
    _scope(env, "agent_deny_all")
    ids = await _recall_ids(_svc(db_session, env), env)
    assert ids == set()


@pytest.mark.asyncio(loop_scope="session")
async def test_shadow_recall_unfiltered_with_would_deny_aggregate(env, db_session):
    _scope(env, "agent_shadow", mode="shadow")
    ids = await _recall_ids(_svc(db_session, env), env)
    # Shadow changes nothing observable — every row still returned.
    assert ids == {env["mem_note"], env["mem_time"], env["mem_api"]}

    rows = (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(
                    MemoryAccessEvent.workspace_id == env["ws_id"],
                    MemoryAccessEvent.policy_decision == "would_deny",
                )
            )
        )
        .scalars()
        .all()
    )
    aggregates = [r for r in rows if (r.event_metadata or {}).get("filter_kind") == ROW_FILTER_KIND]
    assert len(aggregates) == 1
    meta = aggregates[0].event_metadata
    assert meta["would_deny_count"] == 2
    assert set(meta["memory_ids"]) == {str(env["mem_time"]), str(env["mem_api"])}
    assert meta["requested_context_id"] == str(env["ctx"])
    assert aggregates[0].outcome == "success"
    assert aggregates[0].workspace_id == env["ws_id"]
    # Authoritative columns stay NULL — the ids above are claims.
    assert aggregates[0].context_id is None


@pytest.mark.asyncio(loop_scope="session")
async def test_reference_denied_type_uniform_404_with_audit(env, db_session):
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    with pytest.raises(NotFoundException):
        await svc.reference(env["mem_time"], env["uid"])

    rows = (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(
                    MemoryAccessEvent.workspace_id == env["ws_id"],
                    MemoryAccessEvent.operation == "reference",
                    MemoryAccessEvent.outcome == "denied",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    meta = rows[0].event_metadata
    assert rows[0].policy_decision == "binding_denied"
    assert meta["filter_kind"] == ROW_FILTER_KIND
    assert meta["requested_memory_id"] == str(env["mem_time"])
    assert rows[0].memory_id is None  # claim rides metadata only


@pytest.mark.asyncio(loop_scope="session")
async def test_reference_allowed_type_returns_row(env, db_session):
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    response = await svc.reference(env["mem_note"], env["uid"])
    assert response.memory_id == env["mem_note"]


@pytest.mark.asyncio(loop_scope="session")
async def test_forget_by_id_denied_source_stays_silent_empty(env, db_session):
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    response = await svc.forget(
        ForgetRequest(memory_id=env["mem_api"]), env["uid"], current_context_id=None
    )
    assert response.deleted_count == 0

    still_there = await db_session.get(Memory, env["mem_api"])
    assert still_there is not None and still_there.deleted_at is None

    rows = (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(
                    MemoryAccessEvent.workspace_id == env["ws_id"],
                    MemoryAccessEvent.operation == "forget",
                    MemoryAccessEvent.outcome == "denied",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_metadata["filter_kind"] == ROW_FILTER_KIND


@pytest.mark.asyncio(loop_scope="session")
async def test_load_pinned_filters_pinned_set(env, db_session):
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    response = await svc.load_pinned(
        env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
    )
    ids = {m.memory_id for m in response.memories}
    # Both note+time are pinned (delivery_mode=always); only note survives.
    assert ids == {env["mem_note"]}
    # total_available stays the context's pinned-set size (repo count).
    assert response.total_available == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_explore_neighbors_filtered_at_materialization(env, db_session):
    svc = MemoryService(db_session)

    # Baseline (no scope): the time neighbor is reachable via the edge.
    baseline = await svc.explore(
        ExploreRequest(memory_id=env["mem_note"], depth=1, min_weight=0.0),
        env["uid"],
    )
    assert env["mem_time"] in {m.memory_id for m in baseline.related_memories}

    _scope(env, "agent_filtered")
    filtered = await svc.explore(
        ExploreRequest(memory_id=env["mem_note"], depth=1, min_weight=0.0),
        env["uid"],
    )
    assert env["mem_time"] not in {m.memory_id for m in filtered.related_memories}


@pytest.mark.asyncio(loop_scope="session")
async def test_explore_seed_denied_type(env, db_session):
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    with pytest.raises(NotFoundException):
        await svc.explore(
            ExploreRequest(memory_id=env["mem_time"], depth=1, min_weight=0.0),
            env["uid"],
        )


#  Note: forget(query=...)'s enforcement is transitive — it resolves victims
#  through recall(), whose per-row filter is pinned by the recall tests above,
#  so a denied-type row never reaches its deletion loop. A dedicated E2E would
#  need the full Qdrant-delete + edge-cleanup pipeline stubbed; the transitive
#  coverage plus the unit pins on filter_memory_rows_by_binding suffice.


@pytest.mark.asyncio(loop_scope="session")
async def test_reference_linked_refs_filtered(env, db_session):
    # reference()'s declared-link refs expose neighbor type/summary; a
    # denied-type neighbor must not leak through them. note -> time edge
    # exists; the filtered agent references note and must not see the time
    # neighbor in its outgoing links.
    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    response = await svc.reference(env["mem_note"], env["uid"])
    linked_ids = {ref.memory_id for ref in (response.outgoing_links or [])}
    assert env["mem_time"] not in linked_ids


@pytest.mark.asyncio(loop_scope="session")
async def test_shadow_load_pinned_unfiltered(env, db_session):
    _scope(env, "agent_shadow", mode="shadow")
    svc = MemoryService(db_session)
    response = await svc.load_pinned(
        env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
    )
    ids = {m.memory_id for m in response.memories}
    # Shadow keeps every pinned row (note + time both delivery=always).
    assert ids == {env["mem_note"], env["mem_time"]}


@pytest.mark.asyncio(loop_scope="session")
async def test_upcoming_time_lane_filtered(env, db_session):
    from services.time_memory import query_upcoming_time_memories

    baseline = await query_upcoming_time_memories(
        db_session, env["ctx"], q_from=None, q_until=None, k=10
    )
    assert {r["memory_id"] for r in baseline} == {str(env["mem_time"])}

    _scope(env, "agent_filtered")  # binding excludes type 'time'
    filtered = await query_upcoming_time_memories(
        db_session, env["ctx"], q_from=None, q_until=None, k=10
    )
    assert filtered == []
