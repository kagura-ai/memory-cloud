"""End-to-end validation of the v0.49.x agent access-control features against
the REAL ``MemoryService`` + real test DB (no service mocking beyond the Qdrant
search backend).

Exercises the RFC-0002 P0-2 subtractive agent-binding enforcement as one flow —
Agent Registry row + AgentContextBinding + AgentScope → the ``recall`` gate —
including the #1291 fix (the service-layer gate that closed the REST recall
bypass) and the memory_access_events audit emission. This is the flow that would
have caught #1291: an enforce-mode agent bound to one context must be denied on
another, on the recall surface, end to end.

Setup rows are flushed (not committed) on ``db_session`` so they vanish on the
session rollback; the only committed side effect is the fail-open audit writer's
independent session, which the teardown cleans up by workspace_id.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from models.agent import Agent
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import SOURCE_TYPE_MANUAL, Memory
from models.memory_access_event import MemoryAccessEvent
from models.schemas import RecallRequest
from services.agent_binding_service import AgentBindingService
from services.memory_service import MemoryService
from utils.exceptions import NotFoundException


@pytest.fixture(autouse=True)
def _clean_scope():
    # The gate reads a ContextVar; reset before/after every test so no scope
    # leaks between the enforce tests and the backward-compat (no-scope) test.
    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest_asyncio.fixture(loop_scope="session")
async def env(db_session):
    """Build workspace + owner + two contexts + an enforce-mode agent bound to
    ONLY the first context + a seeded memory in each. Flush (no commit)."""
    uid = f"e2e-agent-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()

    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="E2E Agent Owner",
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

    ctx_bound = Context(id=uuid.uuid4(), workspace_id=ws_id, name="bound", created_by=uid)
    ctx_unbound = Context(id=uuid.uuid4(), workspace_id=ws_id, name="unbound", created_by=uid)
    db_session.add_all([ctx_bound, ctx_unbound])
    await db_session.flush()

    agent = Agent(workspace_id=ws_id, name=f"agent-{uuid.uuid4().hex[:6]}", owner_user_id=uid)
    db_session.add(agent)
    await db_session.flush()

    # Bind the agent to ctx_bound ONLY. No binding for ctx_unbound => default
    # deny under enforce.
    await AgentBindingService(db_session).create_binding(
        agent=agent,
        context_id=ctx_bound.id,
        created_by=uid,
        can_read=True,
        write_policy="direct",
        is_default=True,
    )

    mem_bound = Memory(
        id=uuid.uuid4(),
        user_id=uid,
        workspace_id=ws_id,
        context_id=ctx_bound.id,
        summary="bound-context memory",
        content="secret in the bound context",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
    )
    mem_unbound = Memory(
        id=uuid.uuid4(),
        user_id=uid,
        workspace_id=ws_id,
        context_id=ctx_unbound.id,
        summary="unbound-context memory",
        content="secret in the unbound context",
        type="note",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
    )
    db_session.add_all([mem_bound, mem_unbound])
    await db_session.flush()

    yield {
        "uid": uid,
        "ws_id": ws_id,
        "agent": agent,
        "ctx_bound": ctx_bound.id,
        "ctx_unbound": ctx_unbound.id,
        "mem_bound": mem_bound.id,
        "mem_unbound": mem_unbound.id,
    }

    # The audit writer commits on an independent session — clean those rows
    # up. memory_access_events is append-only by trigger (e66), so test
    # cleanup must disable the user triggers for the DELETE (table owner
    # privilege; the trigger exists to stop application-path mutation, not
    # test-harness residue removal). Rollback first: a test that ended in a
    # raised NotFoundException may have left the session tx aborted.
    from sqlalchemy import text

    await db_session.rollback()
    try:
        # DISABLE + DELETE + ENABLE + COMMIT are one transaction: either all
        # persist (triggers re-enabled at commit) or none do. ALTER TABLE ...
        # DISABLE TRIGGER is transactional DDL in PostgreSQL, so the explicit
        # rollback below also rolls the DISABLE back — the append-only
        # invariant can never be left disabled for later tests (Copilot
        # review of #1300).
        await db_session.execute(text("ALTER TABLE memory_access_events DISABLE TRIGGER USER"))
        await db_session.execute(
            delete(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == ws_id)
        )
        await db_session.execute(text("ALTER TABLE memory_access_events ENABLE TRIGGER USER"))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


def _svc(db_session, *, found_memory_ids):
    """Real MemoryService with ONLY the Qdrant search backend stubbed."""
    svc = MemoryService(db_session)
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(mid), "score": 0.9} for mid in found_memory_ids]
    )
    return svc


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_agent_recall_allows_bound_context(env, db_session):
    set_agent_scope(AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce"))
    svc = _svc(db_session, found_memory_ids=[env["mem_bound"]])
    resp = await svc.recall(
        RecallRequest(query="secret", k=10, search_mode="keyword"),
        user_id=env["uid"],
        current_context_id=env["ctx_bound"],
        current_workspace_id=env["ws_id"],
    )
    returned = {str(r.memory_id) for r in resp.results}
    assert str(env["mem_bound"]) in returned


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_agent_recall_denies_unbound_context(env, db_session):
    # The #1291 gate: an enforce-mode agent with no binding for ctx_unbound must
    # be denied at the service layer (uniform NotFoundException), even though the
    # member owner has RBAC access to the context.
    set_agent_scope(AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce"))
    svc = _svc(db_session, found_memory_ids=[env["mem_unbound"]])
    with pytest.raises(NotFoundException):
        await svc.recall(
            RecallRequest(query="secret", k=10, search_mode="keyword"),
            user_id=env["uid"],
            current_context_id=env["ctx_unbound"],
            current_workspace_id=env["ws_id"],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_agent_cross_context_recall_denies_when_any_unbound(env, db_session):
    set_agent_scope(AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce"))
    svc = _svc(db_session, found_memory_ids=[env["mem_bound"]])
    with pytest.raises(NotFoundException):
        await svc.recall(
            RecallRequest(query="secret", k=10, search_mode="keyword"),
            user_id=env["uid"],
            current_context_id=env["ctx_bound"],
            current_workspace_id=env["ws_id"],
            context_ids=[env["ctx_bound"], env["ctx_unbound"]],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_no_agent_scope_recall_unbound_succeeds(env, db_session):
    # Backward-compat: a non-agent credential (no scope) is a structural no-op —
    # the binding gate must NOT block it.
    svc = _svc(db_session, found_memory_ids=[env["mem_unbound"]])
    resp = await svc.recall(
        RecallRequest(query="secret", k=10, search_mode="keyword"),
        user_id=env["uid"],
        current_context_id=env["ctx_unbound"],
        current_workspace_id=env["ws_id"],
    )
    returned = {str(r.memory_id) for r in resp.results}
    assert str(env["mem_unbound"]) in returned


@pytest.mark.asyncio(loop_scope="session")
async def test_recall_under_agent_identity_writes_memory_access_event(env, db_session):
    # The audit feature (#1278): a recall under verified agent identity writes an
    # append-only memory_access_events row (operation=recall, agent_id, success).
    set_agent_scope(AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce"))
    svc = _svc(db_session, found_memory_ids=[env["mem_bound"]])
    await svc.recall(
        RecallRequest(query="secret", k=10, search_mode="keyword"),
        user_id=env["uid"],
        current_context_id=env["ctx_bound"],
        current_workspace_id=env["ws_id"],
    )
    # Writer uses an independent committed session; read it back fresh.
    rows = (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == env["ws_id"])
            )
        )
        .scalars()
        .all()
    )
    recall_rows = [r for r in rows if r.operation == "recall"]
    assert recall_rows, "recall under agent identity must emit a memory_access_events row"
    assert recall_rows[0].agent_id == env["agent"].id
    assert recall_rows[0].outcome == "success"


# ---------------------------------------------------------------------------
# #1286 item 2 (P0-5): deny capture — the decisions the log line used to
# promise are now durable rows, end to end against the real DB.
# ---------------------------------------------------------------------------


async def _mae_rows(db_session, ws_id):
    return (
        (
            await db_session.execute(
                select(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_deny_persists_binding_denied_row(env, db_session):
    # The uniform 404 stays the response; the durable record is the new part.
    set_agent_scope(
        AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce", workspace_id=env["ws_id"])
    )
    svc = _svc(db_session, found_memory_ids=[env["mem_unbound"]])
    with pytest.raises(NotFoundException):
        await svc.recall(
            RecallRequest(query="secret", k=10, search_mode="keyword"),
            user_id=env["uid"],
            current_context_id=env["ctx_unbound"],
            current_workspace_id=env["ws_id"],
        )

    denied = [r for r in await _mae_rows(db_session, env["ws_id"]) if r.outcome == "denied"]
    assert len(denied) == 1, "the hard deny must persist exactly one denied row"
    row = denied[0]
    assert row.operation == "recall"
    assert row.policy_decision == "binding_denied"
    assert row.agent_id == env["agent"].id
    # workspace_id = the CREDENTIAL scope; the requested (denied) context
    # never reaches the authoritative column — it rides event_metadata.
    assert row.workspace_id == env["ws_id"]
    assert row.context_id is None
    assert row.event_metadata["requested_context_id"] == str(env["ctx_unbound"])
    assert row.event_metadata["access"] == "read"


@pytest.mark.asyncio(loop_scope="session")
async def test_shadow_would_deny_persists_ramp_row_and_proceeds(env, db_session):
    # Shadow mode: the request proceeds AND leaves the would_deny ramp signal
    # — exactly one (no double-count), alongside the normal success row.
    set_agent_scope(
        AgentScope(agent_id=env["agent"].id, enforcement_mode="shadow", workspace_id=env["ws_id"])
    )
    svc = _svc(db_session, found_memory_ids=[env["mem_unbound"]])
    resp = await svc.recall(
        RecallRequest(query="secret", k=10, search_mode="keyword"),
        user_id=env["uid"],
        current_context_id=env["ctx_unbound"],
        current_workspace_id=env["ws_id"],
    )
    assert str(env["mem_unbound"]) in {str(r.memory_id) for r in resp.results}

    rows = await _mae_rows(db_session, env["ws_id"])
    ramp = [r for r in rows if r.policy_decision == "would_deny"]
    assert len(ramp) == 1, "shadow must persist exactly one would_deny row"
    assert ramp[0].operation == "recall"
    assert ramp[0].outcome == "success"  # the request DID proceed
    assert ramp[0].event_metadata["requested_context_id"] == str(env["ctx_unbound"])
    # The op's own success row still lands, unstamped.
    plain = [r for r in rows if r.operation == "recall" and r.policy_decision is None]
    assert plain, "the normal recall success row must still be emitted"
    assert all(r.outcome != "denied" for r in rows)


@pytest.mark.asyncio(loop_scope="session")
async def test_forget_by_id_silent_deny_leaves_its_only_record(env, db_session):
    # forget-by-id deny returns a success-shaped empty response (CWE-639
    # posture) — before #1286 the deny left NO trace. The denied row is its
    # only record.
    from models.schemas import ForgetRequest

    set_agent_scope(
        AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce", workspace_id=env["ws_id"])
    )
    svc = MemoryService(db_session)
    resp = await svc.forget(ForgetRequest(memory_id=env["mem_unbound"]), env["uid"])
    assert resp.deleted_count == 0 and resp.memory_ids == []

    rows = await _mae_rows(db_session, env["ws_id"])
    denied = [r for r in rows if r.outcome == "denied"]
    assert len(denied) == 1
    row = denied[0]
    assert row.operation == "forget"
    assert row.policy_decision == "binding_denied"
    assert row.memory_id is None  # requested id is a claim, not a column
    assert row.event_metadata["requested_memory_id"] == str(env["mem_unbound"])
    # No success row — the delete never happened.
    assert not [r for r in rows if r.operation == "forget" and r.outcome == "success"]


@pytest.mark.asyncio(loop_scope="session")
async def test_shadow_forget_by_id_declared_context_single_would_deny(env, db_session):
    # Review finding (#1286): forget-by-id WITH a declared context traverses
    # TWO service-layer binding gates (declared-context isolation params +
    # can_access_memory) — the writer's request-scoped dedup must collapse
    # the shadow signal to exactly one would_deny row, or the shadow→enforce
    # ramp metric double-counts every such request.
    from models.schemas import ForgetRequest

    set_agent_scope(
        AgentScope(agent_id=env["agent"].id, enforcement_mode="shadow", workspace_id=env["ws_id"])
    )
    svc = MemoryService(db_session)
    with patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()):
        resp = await svc.forget(
            ForgetRequest(memory_id=env["mem_unbound"]),
            env["uid"],
            current_context_id=env["ctx_unbound"],
        )
    assert resp.deleted_count == 1  # shadow proceeds

    rows = await _mae_rows(db_session, env["ws_id"])
    ramp = [r for r in rows if r.policy_decision == "would_deny" and r.operation == "forget"]
    assert len(ramp) == 1, f"expected the single deduped shadow row, got {len(ramp)}"
    # The destructive write itself is also audited (success row).
    assert [r for r in rows if r.operation == "forget" and r.outcome == "success"]


@pytest.mark.asyncio(loop_scope="session")
async def test_read_pre_gate_enforce_deny_persists_row(env, db_session):
    # Review finding (#1286, the #1291/#1292 parity class): the MCP read face
    # resolves via resolve_context_for_workspace_read, whose binding filter
    # raises the uniform 404 BEFORE any service-layer gate — with operation
    # threaded it must persist the denied row itself.
    from services.permission_service import PermissionService

    set_agent_scope(
        AgentScope(agent_id=env["agent"].id, enforcement_mode="enforce", workspace_id=env["ws_id"])
    )
    with pytest.raises(NotFoundException):
        await PermissionService(db_session).resolve_context_for_workspace_read(
            user_id=env["uid"], context_id=env["ctx_unbound"], operation="recall"
        )

    rows = await _mae_rows(db_session, env["ws_id"])
    denied = [r for r in rows if r.outcome == "denied"]
    assert len(denied) == 1
    assert denied[0].operation == "recall"
    assert denied[0].policy_decision == "binding_denied"
    assert denied[0].workspace_id == env["ws_id"]
    assert denied[0].context_id is None
    assert denied[0].event_metadata["requested_context_id"] == str(env["ctx_unbound"])
