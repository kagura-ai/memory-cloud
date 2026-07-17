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
    # #1301: read-denied at the CONTEXT level (can_read=False, no arrays) —
    # the enumeration surfaces must subtract this whole context, not just
    # type/source-denied rows.
    agent_no_read = Agent(
        workspace_id=ws_id, name=f"noread-{uuid.uuid4().hex[:6]}", owner_user_id=uid
    )
    db_session.add_all([agent_filtered, agent_deny_all, agent_shadow, agent_no_read])
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
    await svc.create_binding(
        agent=agent_no_read,
        context_id=ctx.id,
        created_by=uid,
        can_read=False,
        write_policy="direct",
        is_default=True,
    )

    # #1301: a second context the agents have NO binding on — default-deny
    # must keep its rows out of the unscoped enumeration/aggregate surfaces.
    ctx_unbound = Context(id=uuid.uuid4(), workspace_id=ws_id, name="unbound", created_by=uid)
    db_session.add(ctx_unbound)
    await db_session.flush()

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
    mem_unbound = Memory(
        id=uuid.uuid4(),
        user_id=uid,
        workspace_id=ws_id,
        context_id=ctx_unbound.id,
        summary="unbound context row",
        content="content of unbound row",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
    )
    db_session.add_all([mem_note, mem_time, mem_api, mem_unbound])
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
        "ctx_unbound": ctx_unbound.id,
        "agent_filtered": agent_filtered.id,
        "agent_deny_all": agent_deny_all.id,
        "agent_shadow": agent_shadow.id,
        "agent_no_read": agent_no_read.id,
        "mem_note": mem_note.id,
        "mem_time": mem_time.id,
        "mem_api": mem_api.id,
        "mem_unbound": mem_unbound.id,
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


# ---------------------------------------------------------------------------
# #1301: enumeration / aggregate / update-response read surfaces.
# Same env, same doctrine: enforce subtracts, shadow changes nothing
# observable, non-agent credentials are untouched. These surfaces are
# outside the MAE operation vocabulary, so enforcement here is log-only
# (no would_deny rows) — the recall/load_pinned lanes above pin the
# audit shape for the ramp.
# ---------------------------------------------------------------------------


async def _list_route(env, db_session, **overrides):
    """Call the /memory/list route handler directly (route-owned SQL —
    there is no service method to test below it)."""
    from api.routes.memory import list_memories

    kwargs = {
        "user": {"user_id": env["uid"]},
        "db": db_session,
        "scope": None,
        "type": None,
        "context_id": env["ctx"],
        "q": None,
        "tags": None,
        "tags_match": "any",
        "trigger_from": None,
        "trigger_until": None,
        "order_by": "created_at",
        "limit": 50,
        "offset": 0,
    }
    kwargs.update(overrides)
    return await list_memories(**kwargs)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_no_scope_returns_all_rows(env, db_session):
    response = await _list_route(env, db_session)
    assert {uuid.UUID(m.id) for m in response.memories} == {
        env["mem_note"],
        env["mem_time"],
        env["mem_api"],
    }
    assert response.total == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_enforce_filters_rows_and_total(env, db_session):
    _scope(env, "agent_filtered")
    response = await _list_route(env, db_session)
    assert {uuid.UUID(m.id) for m in response.memories} == {env["mem_note"]}
    # total must not act as an existence oracle over denied rows.
    assert response.total == 1
    assert response.has_more is False


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_shadow_unchanged(env, db_session):
    _scope(env, "agent_shadow", mode="shadow")
    response = await _list_route(env, db_session)
    assert len(response.memories) == 3
    assert response.total == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_enforce_excludes_unbound_context(env, db_session):
    # Unscoped "my memories" view: the non-agent owner sees rows from every
    # context; an enforce-mode agent must not enumerate contexts it has no
    # binding on (P0-2 default-deny — recall 404s these outright).
    baseline = await _list_route(env, db_session, context_id=None)
    assert env["mem_unbound"] in {uuid.UUID(m.id) for m in baseline.memories}
    assert baseline.total == 4

    _scope(env, "agent_filtered")
    response = await _list_route(env, db_session, context_id=None)
    assert {uuid.UUID(m.id) for m in response.memories} == {env["mem_note"]}
    assert response.total == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_scoped_can_read_false_uniform_404(env, db_session):
    # Scoped list goes through resolve_context_for_workspace_read, whose
    # #1275 agent gate already denies can_read=False with the uniform 404.
    _scope(env, "agent_no_read")
    with pytest.raises(NotFoundException):
        await _list_route(env, db_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_route_unscoped_can_read_false_context_empty(env, db_session):
    # The UNSCOPED list has no context chokepoint — the SQL predicate's
    # membership gate must subtract the read-denied context's rows itself.
    _scope(env, "agent_no_read")
    response = await _list_route(env, db_session, context_id=None)
    assert response.memories == []
    assert response.total == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_stats_enforce_excludes_unbound_and_no_read_contexts(env, db_session):
    svc = MemoryService(db_session)

    # Workspace-wide stats: unbound-context rows must not be counted.
    _scope(env, "agent_filtered")
    stats = await svc.get_stats(env["uid"], workspace_id=str(env["ws_id"]))
    assert stats.by_type == {"note": 1}
    assert stats.total_count == 1

    # can_read=False: the bound context contributes nothing either.
    _scope(env, "agent_no_read")
    stats = await svc.get_stats(env["uid"], workspace_id=str(env["ws_id"]))
    assert stats.total_count == 0
    assert stats.by_type == {}


@pytest_asyncio.fixture(loop_scope="session")
async def access_seeded(env, db_session):
    """most_accessed only surfaces rows with last_used_at set."""
    from utils.datetime import utcnow

    for key in ("mem_note", "mem_time", "mem_api"):
        row = await db_session.get(Memory, env[key])
        row.last_used_at = utcnow()
    await db_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_access_patterns_enforce_filters_rows_and_distribution(
    env, access_seeded, db_session
):
    from api.routes.memory import get_access_patterns

    baseline = await get_access_patterns(
        user={"user_id": env["uid"]}, context_id=env["ctx"], db=db_session, days=30
    )
    assert baseline["type_distribution"] == {"note": 2, "time": 1}
    assert len(baseline["most_accessed"]) == 3

    _scope(env, "agent_filtered")
    filtered = await get_access_patterns(
        user={"user_id": env["uid"]}, context_id=env["ctx"], db=db_session, days=30
    )
    # mem_time is type-denied, mem_api is source-denied.
    assert filtered["type_distribution"] == {"note": 1}
    assert {m["memory_id"] for m in filtered["most_accessed"]} == {str(env["mem_note"])}
    assert filtered["recent_access_count"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_access_patterns_shadow_unchanged(env, access_seeded, db_session):
    from api.routes.memory import get_access_patterns

    _scope(env, "agent_shadow", mode="shadow")
    response = await get_access_patterns(
        user={"user_id": env["uid"]}, context_id=env["ctx"], db=db_session, days=30
    )
    assert response["type_distribution"] == {"note": 2, "time": 1}
    assert len(response["most_accessed"]) == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_access_patterns_unscoped_can_read_false_context_empty(
    env, access_seeded, db_session
):
    from api.routes.memory import get_access_patterns

    # Unscoped access-patterns has no context chokepoint (scoped goes through
    # the resolver's #1275 gate) — the membership gate must empty it.
    _scope(env, "agent_no_read")
    response = await get_access_patterns(
        user={"user_id": env["uid"]}, context_id=None, db=db_session, days=30
    )
    assert response["most_accessed"] == []
    assert response["type_distribution"] == {}
    assert response["recent_access_count"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_stats_enforce_intersects_all_aggregates(env, db_session):
    svc = MemoryService(db_session)
    baseline = await svc.get_stats(
        env["uid"], workspace_id=str(env["ws_id"]), context_id=str(env["ctx"])
    )
    assert baseline.by_type == {"note": 2, "time": 1}
    assert baseline.total_count == 3

    _scope(env, "agent_filtered")
    stats = await svc.get_stats(
        env["uid"], workspace_id=str(env["ws_id"]), context_id=str(env["ctx"])
    )
    # Only note/manual survives — the aggregate is not an existence oracle.
    assert stats.by_type == {"note": 1}
    assert stats.total_count == 1
    # All seeded rows are scope='working' (direct inserts bypass
    # pin-on-write): the filtered working-count must be 1, not the
    # unfiltered 3 — this pins the scope sub-count query independently.
    assert stats.working_count == 1
    assert stats.persistent_count == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_stats_deny_all_agent_sees_zero(env, db_session):
    _scope(env, "agent_deny_all")
    stats = await MemoryService(db_session).get_stats(
        env["uid"], workspace_id=str(env["ws_id"]), context_id=str(env["ctx"])
    )
    assert stats.total_count == 0
    assert stats.by_type == {}


@pytest.mark.asyncio(loop_scope="session")
async def test_stats_shadow_unchanged(env, db_session):
    _scope(env, "agent_shadow", mode="shadow")
    stats = await MemoryService(db_session).get_stats(
        env["uid"], workspace_id=str(env["ws_id"]), context_id=str(env["ctx"])
    )
    assert stats.by_type == {"note": 2, "time": 1}
    assert stats.total_count == 3


@pytest_asyncio.fixture(loop_scope="session")
async def cluster_env(env, db_session):
    """A succeeded analysis run whose single cluster contains all three
    env memories, with the type-denied row as a representative."""
    from datetime import datetime

    from models.analysis import (
        MemoryAnalysis,
        MemoryAnalysisAssignment,
        MemoryAnalysisCluster,
    )
    from models.llm_pricing import LLMPricing
    from utils.datetime import utcnow

    pricing = LLMPricing(
        provider="openai",
        model="gpt-5-nano",
        unit_type="input_tokens",
        price_per_unit=0.20,
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(pricing)
    await db_session.flush()

    run = MemoryAnalysis(
        id=uuid.uuid4(),
        workspace_id=env["ws_id"],
        context_id=env["ctx"],
        triggered_by=env["uid"],
        status="succeeded",
        started_at=utcnow(),
        finished_at=utcnow(),
        model_id=pricing.id,
        model_snapshot={"model": "gpt-5-nano", "rates": {}},
        embedding_model="text-embedding-3-small",
        params={},
        input_count=3,
        paid_by="byok",
    )
    db_session.add(run)
    await db_session.flush()

    cluster = MemoryAnalysisCluster(
        id=uuid.uuid4(),
        analysis_id=run.id,
        cluster_index=0,
        label="all rows",
        description=None,
        count=3,
        centroid_2d=[0.0, 0.0],
        representative_memory_ids=[env["mem_time"], env["mem_note"]],
        property_stats={"types": {"note": 2, "time": 1}, "avg_importance": 0.5},
        label_confidence=0.9,
    )
    db_session.add(cluster)
    await db_session.flush()

    for i, key in enumerate(("mem_note", "mem_time", "mem_api")):
        db_session.add(
            MemoryAnalysisAssignment(
                analysis_id=run.id,
                memory_id=env[key],
                cluster_id=cluster.id,
                x=float(i),
                y=0.0,
            )
        )
    await db_session.flush()
    return {"run_id": run.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_get_cluster_enforce_filters_members_and_representatives(
    env, cluster_env, db_session
):
    from services.analysis import query_service

    baseline = await query_service.get_cluster(
        db_session, workspace_id=env["ws_id"], run_id=cluster_env["run_id"], cluster_index=0
    )
    assert {m["memory_id"] for m in baseline["memories"]} == {
        str(env["mem_note"]),
        str(env["mem_time"]),
        str(env["mem_api"]),
    }
    assert {r["memory_id"] for r in baseline["representatives"]} == {
        str(env["mem_time"]),
        str(env["mem_note"]),
    }

    _scope(env, "agent_filtered")
    filtered = await query_service.get_cluster(
        db_session, workspace_id=env["ws_id"], run_id=cluster_env["run_id"], cluster_index=0
    )
    assert {m["memory_id"] for m in filtered["memories"]} == {str(env["mem_note"])}
    assert {r["memory_id"] for r in filtered["representatives"]} == {str(env["mem_note"])}
    # The stored whole-cluster aggregates are the same grouped-count
    # existence oracle stats.by_type was — they must be recomputed over the
    # rows the agent can read (mem_api is source-denied, so note counts 1).
    assert filtered["count"] == 1
    assert filtered["property_stats"]["types"] == {"note": 1}
    # Non-type facets stay as stored (not type/source-labeled).
    assert filtered["property_stats"]["avg_importance"] == 0.5


@pytest.mark.asyncio(loop_scope="session")
async def test_get_cluster_shadow_unchanged(env, cluster_env, db_session):
    from services.analysis import query_service

    _scope(env, "agent_shadow", mode="shadow")
    response = await query_service.get_cluster(
        db_session, workspace_id=env["ws_id"], run_id=cluster_env["run_id"], cluster_index=0
    )
    assert len(response["memories"]) == 3
    assert len(response["representatives"]) == 2
    assert response["count"] == 3
    assert response["property_stats"]["types"] == {"note": 2, "time": 1}


@pytest.mark.asyncio(loop_scope="session")
async def test_forget_skip_flag_rejected_for_query_mode(env, db_session):
    # The internal bypass only covers the memory_id branch; forget(query=...)
    # resolves victims through recall() where the flag cannot propagate. A
    # caller combining them would get silent partial maintenance — reject it
    # loudly instead.
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="_skip_binding_row_filter"):
        await svc.forget(
            ForgetRequest(query="anything"),
            env["uid"],
            current_context_id=env["ctx"],
            _skip_binding_row_filter=True,
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_denied_type_uniform_404(env, db_session):
    from models.schemas import PatchMemoryRequest

    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    # A no-op PATCH on a read-denied row must not read it back — id-addressed
    # ops on denied rows are uniformly 404 (the #1299 forget/reference doctrine).
    with pytest.raises(NotFoundException):
        await svc.patch_memory(env["mem_time"], PatchMemoryRequest(importance=0.9), env["uid"])


@pytest.mark.asyncio(loop_scope="session")
async def test_update_in_place_denied_type_uniform_404(env, db_session):
    from models.schemas import UpdateMemoryRequest

    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    with pytest.raises(NotFoundException):
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=env["mem_api"], importance=0.9),
            user_id=env["uid"],
            current_context_id=env["ctx"],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_replacement_forget_bypasses_row_filter(env, db_session):
    """#1301: the upsert's internal replacement delete is maintenance, not an
    agent read — a denied-type existing row must still be replaced, never
    left as a live duplicate alongside the new row."""
    from unittest.mock import patch

    from models.schemas import RememberResponse, UpdateMemoryRequest

    ext = f"ext-{uuid.uuid4().hex[:8]}"
    old = Memory(
        id=uuid.uuid4(),
        user_id=env["uid"],
        workspace_id=env["ws_id"],
        context_id=env["ctx"],
        summary="old external row",
        content="old content",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_API,  # source-denied for agent_filtered
        details={"resource_id": ext},
    )
    db_session.add(old)
    await db_session.flush()

    _scope(env, "agent_filtered")
    svc = MemoryService(db_session)
    svc.remember = AsyncMock(return_value=RememberResponse(memory_id=uuid.uuid4(), scope="working"))
    with patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()):
        response = await svc.update_memory(
            UpdateMemoryRequest(
                external_id=ext,
                summary="replacement summary text",
                content="replacement content",
                type="note",
            ),
            user_id=env["uid"],
            current_context_id=env["ctx"],
            current_workspace_id=env["ws_id"],
        )

    assert response.operation == "replaced"
    refreshed = await db_session.get(Memory, old.id)
    assert refreshed.deleted_at is not None, (
        "internal replacement forget must bypass the per-memory read filter — "
        "a denied-type row left live creates duplicates per external_id"
    )
