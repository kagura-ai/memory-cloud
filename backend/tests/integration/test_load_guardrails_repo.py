"""Integration pins for the deterministic guardrail-set read (``load_guardrails``).

Mirrors ``test_load_pinned_repo.py`` and ``test_bootstrap_trusted_tier_e2e.py``
against the real DB. ``MemoryRepository.list_tool_triggered`` is the
determinism-critical read path of the tool-guardrail lane:

* the trust gate is UNCONDITIONAL — rows from an external-tier context and
  connector-ingested rows are excluded with no flag to turn it off;
* the order is ``importance DESC, created_at ASC, id ASC`` and the cap sets
  ``truncated`` / ``total_available`` exactly;
* only rows carrying ``details.tool_trigger`` are selected — and a raw-SQL
  JSON ``null`` IS selected by the ``json`` path predicate (which is why the
  write path removes the key instead of storing ``null``);
* the service lane never touches the embedding client or the vector store,
  applies the per-memory binding filter, and returns the uniform
  ``context_not_found`` on a denied context.

Setup rows are flushed (not committed) on ``db_session`` so they vanish on the
session rollback. The ``memory_access_events`` writer is a no-op without a
verified agent identity, so the only committed side effects are the binding
rows the ``AgentBindingService`` writes (deleted at teardown).

Not executed in the local unit run — needs the DB container
(``make test-integration``).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, text

from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import WorkspaceRole
from models.agent import Agent
from models.auth import (
    CONTEXT_TRUST_TIER_EXTERNAL,
    Context,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import (
    DELIVERY_MODE_ALWAYS,
    SOURCE_TYPE_CONNECTOR,
    SOURCE_TYPE_MANUAL,
    Memory,
)
from repositories.memory import MemoryRepository
from services.agent_binding_service import AgentBindingService
from services.memory_service import MemoryService
from utils.datetime import utcnow
from utils.exceptions import NotFoundException

TT = {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge", "action": "inform"}


def _mem(uid, ws_id, ctx_id, *, summary, source_type=SOURCE_TYPE_MANUAL, **overrides):
    fields = {
        "id": uuid.uuid4(),
        "user_id": uid,
        "workspace_id": ws_id,
        "context_id": ctx_id,
        "summary": summary,
        "content": f"FULL CONTENT of {summary} — never in the guardrail read",
        "type": "troubleshooting",
        "client": "test",
        "tags": [],
        "source_type": source_type,
        "details": {"tool_trigger": dict(TT)},
    }
    fields.update(overrides)
    return Memory(**fields)


@pytest.fixture(autouse=True)
def _clean_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest_asyncio.fixture(loop_scope="session")
async def env(db_session):
    """Owner + outsider, a trusted and an external context, guardrail rows of
    every provenance, plus one agent bound with ``allowed_memory_types=['note']``."""
    uid = f"e2e-guard-{uuid.uuid4().hex[:8]}"
    outsider = f"e2e-guard-out-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()

    for user_id, name in ((uid, "Guardrail Owner"), (outsider, "Guardrail Outsider")):
        db_session.add(
            User(
                email=f"{user_id}@example.test",
                user_id=user_id,
                name=name,
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

    ctx = Context(id=uuid.uuid4(), workspace_id=ws_id, name="trusted", created_by=uid)
    ctx_ext = Context(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        name="external",
        created_by=uid,
        trust_tier=CONTEXT_TRUST_TIER_EXTERNAL,
    )
    db_session.add_all([ctx, ctx_ext])
    await db_session.flush()

    # Order fixtures: importance DESC, then created_at ASC, then id ASC.
    high = _mem(uid, ws_id, ctx.id, summary="high", importance=0.9, type="note")
    early = _mem(uid, ws_id, ctx.id, summary="early", importance=0.5)
    late = _mem(uid, ws_id, ctx.id, summary="late", importance=0.5)
    connector = _mem(uid, ws_id, ctx.id, summary="connector", source_type=SOURCE_TYPE_CONNECTOR)
    external = _mem(uid, ws_id, ctx_ext.id, summary="external")
    plain = _mem(uid, ws_id, ctx.id, summary="plain", details={"other": 1})
    pinned_only = _mem(
        uid, ws_id, ctx.id, summary="pinned-only", details=None, delivery_mode=DELIVERY_MODE_ALWAYS
    )
    both = _mem(uid, ws_id, ctx.id, summary="both", delivery_mode=DELIVERY_MODE_ALWAYS)
    gone = _mem(uid, ws_id, ctx.id, summary="gone", deleted_at=utcnow())
    db_session.add_all([high, early, late, connector, external, plain, pinned_only, both, gone])
    await db_session.flush()
    # created_at ordering between the two 0.5 rows must be deterministic.
    await db_session.execute(
        text("UPDATE memories SET created_at = '2026-06-01T00:00:00' WHERE id = :id"),
        {"id": early.id},
    )
    await db_session.execute(
        text("UPDATE memories SET created_at = '2026-06-02T00:00:00' WHERE id = :id"),
        {"id": late.id},
    )
    await db_session.execute(
        text("UPDATE memories SET created_at = '2026-06-03T00:00:00' WHERE id = :id"),
        {"id": both.id},
    )

    agent = Agent(workspace_id=ws_id, name=f"filtered-{uuid.uuid4().hex[:6]}", owner_user_id=uid)
    db_session.add(agent)
    await db_session.flush()
    await AgentBindingService(db_session).create_binding(
        agent=agent,
        context_id=ctx.id,
        created_by=uid,
        can_read=True,
        write_policy="direct",
        is_default=True,
        allowed_memory_types=["note"],
    )

    yield {
        "uid": uid,
        "outsider": outsider,
        "ws_id": ws_id,
        "ctx": ctx.id,
        "ctx_ext": ctx_ext.id,
        "agent_id": agent.id,
        "high": high.id,
        "early": early.id,
        "late": late.id,
        "both": both.id,
        "pinned_only": pinned_only.id,
        "connector": connector.id,
        "external": external.id,
        "plain": plain.id,
        "gone": gone.id,
    }

    from models.agent import AgentContextBinding

    await db_session.execute(
        delete(AgentContextBinding).where(AgentContextBinding.agent_id == agent.id)
    )
    await db_session.commit()


# --------------------------------------------------------------------------- #
# repo: list_tool_triggered
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_only_trusted_manual_guardrail_rows_in_deterministic_order(env, db_session):
    rows, total = await MemoryRepository(db_session).list_tool_triggered(
        env["ws_id"], env["ctx"], limit=100
    )
    assert total == 4
    # importance DESC (high 0.9), then created_at ASC among the 0.5 rows.
    assert [r.id for r in rows] == [env["high"], env["early"], env["late"], env["both"]]
    ids = {r.id for r in rows}
    assert env["connector"] not in ids  # row-level provenance gate
    assert env["plain"] not in ids  # no tool_trigger key
    assert env["pinned_only"] not in ids  # pinned is the OTHER lane
    assert env["gone"] not in ids  # soft-deleted
    # L1 + projected trigger only; no L3, no context_summary.
    first = rows[0]
    tt = first.tool_trigger
    tt = json.loads(tt) if isinstance(tt, str) else tt  # json element as text or dict
    assert tt == TT
    assert not hasattr(first, "content")
    assert not hasattr(first, "context_summary")


@pytest.mark.asyncio(loop_scope="session")
async def test_external_tier_context_returns_nothing(env, db_session):
    rows, total = await MemoryRepository(db_session).list_tool_triggered(
        env["ws_id"], env["ctx_ext"], limit=100
    )
    assert rows == [] and total == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_cap_bounds_rows_but_total_is_accurate(env, db_session):
    rows, total = await MemoryRepository(db_session).list_tool_triggered(
        env["ws_id"], env["ctx"], limit=2
    )
    assert [r.id for r in rows] == [env["high"], env["early"]]
    assert total == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_raw_json_null_is_selected_by_the_json_path_predicate(env, db_session):
    """``details`` is PostgreSQL ``json``: ``'{"tool_trigger": null}' -> 'tool_trigger'``
    is a JSON ``null`` value, NOT SQL NULL, so the predicate (and the partial
    index) treat it as present. This is exactly why ``normalize_tool_trigger``
    REMOVES the key on an explicit null instead of storing it; the service maps
    the non-object to ``tool_trigger: null`` and consumers skip such an item."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO memories
              (id, user_id, workspace_id, context_id, summary, content, type, importance,
               confidence, scope, embedding_status, client, source, long_term, access_count,
               source_type, details)
            VALUES
              (:id, :uid, :ws, :ctx, 'raw null', 'c', 'note', 0.1, 1.0, 'working', 'success',
               'test', 'mcp_remember', false, 0, 'manual', CAST(:details AS JSON))
            """
        ),
        {
            "id": mem_id,
            "uid": env["uid"],
            "ws": env["ws_id"],
            "ctx": env["ctx"],
            "details": '{"tool_trigger": null}',
        },
    )
    rows, total = await MemoryRepository(db_session).list_tool_triggered(
        env["ws_id"], env["ctx"], limit=100
    )
    assert mem_id in {r.id for r in rows}
    assert total == 5
    resp = await MemoryService(db_session).load_guardrails(
        env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
    )
    raw = next(i for i in resp.tool_triggered if i.memory_id == mem_id)
    assert raw.tool_trigger is None


# --------------------------------------------------------------------------- #
# service: load_guardrails
# --------------------------------------------------------------------------- #


def _svc_that_must_not_search(db_session) -> MemoryService:
    svc = MemoryService(db_session)
    svc.embedding_service.generate_embedding = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("embedding client called on the guardrail lane")
    )
    svc.search_service.hybrid_search = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("search called on the guardrail lane")
    )
    return svc


@pytest.mark.asyncio(loop_scope="session")
async def test_service_serves_both_lanes_without_embedding_or_vector_store(env, db_session):
    svc = _svc_that_must_not_search(db_session)
    with patch("db.qdrant.search_memories_qdrant", side_effect=AssertionError("qdrant called")):
        resp = await svc.load_guardrails(
            env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
        )
    assert resp.format == 1
    assert len(resp.version) == 16
    assert [i.memory_id for i in resp.tool_triggered] == [
        env["high"],
        env["early"],
        env["late"],
        env["both"],
    ]
    # Pinned lane: trusted list_pinned — the both-lanes row appears in both lists.
    pinned_ids = [i.memory_id for i in resp.pinned]
    assert set(pinned_ids) == {env["pinned_only"], env["both"]}
    assert resp.pinned_total_available == 2
    assert resp.tool_triggered_total_available == 4
    assert resp.total_available == 6
    assert resp.truncated is False
    both_tool = next(i for i in resp.tool_triggered if i.memory_id == env["both"])
    both_pinned = next(i for i in resp.pinned if i.memory_id == env["both"])
    assert both_tool.tool_trigger == TT and both_tool.context_summary is None
    assert both_pinned.tool_trigger is None
    assert all(i.authored_by_caller for i in (*resp.pinned, *resp.tool_triggered))


@pytest.mark.asyncio(loop_scope="session")
async def test_service_cap_applies_to_the_tool_lane_only(env, db_session):
    resp = await MemoryService(db_session).load_guardrails(
        env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"], cap=1
    )
    assert [i.memory_id for i in resp.tool_triggered] == [env["high"]]
    assert resp.tool_triggered_truncated is True
    assert resp.tool_triggered_total_available == 4
    assert resp.cap == 1
    assert len(resp.pinned) == 2  # untouched by cap
    assert resp.pinned_truncated is False
    assert resp.truncated is True


@pytest.mark.asyncio(loop_scope="session")
async def test_service_binding_filter_narrows_rows_not_totals(env, db_session):
    set_agent_scope(
        AgentScope(agent_id=env["agent_id"], enforcement_mode="enforce", workspace_id=env["ws_id"])
    )
    resp = await MemoryService(db_session).load_guardrails(
        env["uid"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
    )
    # Only the type='note' guardrail survives the binding; the repo count stays.
    assert [i.memory_id for i in resp.tool_triggered] == [env["high"]]
    assert resp.tool_triggered_total_available == 4
    assert resp.pinned == []  # the pinned rows are type='troubleshooting'
    assert resp.pinned_total_available == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_denied_context_is_the_uniform_not_found(env, db_session):
    with pytest.raises(NotFoundException, match="Context"):
        await MemoryService(db_session).load_guardrails(
            env["outsider"], current_context_id=env["ctx"], current_workspace_id=env["ws_id"]
        )
