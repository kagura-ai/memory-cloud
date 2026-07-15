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
from unittest.mock import AsyncMock

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

    # The audit writer commits on an independent session — clean those rows up.
    await db_session.execute(
        delete(MemoryAccessEvent).where(MemoryAccessEvent.workspace_id == ws_id)
    )
    await db_session.commit()


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
