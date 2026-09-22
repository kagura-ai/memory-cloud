"""Integration pins for the hookless guardrail digest (#1621).

Mirrors ``test_load_guardrails_repo.py`` against the real DB. The digest's
entry source (``services.guardrail_digest.fetch_entries`` /
``fetch_entries_for_context``) reads through the same repo lane as
``load_guardrails`` and must inherit every one of its properties:

* the trust gate is unconditional — external-tier contexts and
  connector-ingested rows yield no entries and ``total_available == 0``;
* a workspace-scoped key for workspace A gets ``None`` for a context in B
  (uniform deny, no signal);
* the per-memory agent-binding filter narrows the rows but not the total;
* the read never touches the embedding client or the vector store;
* a resolver deny for an agent credential writes the ``memory_access_events``
  deny row (``operation="load_guardrails"``), a success writes nothing;
* a memory that is both pinned and tool-triggered is present in
  ``get_context_info.guardrails.items`` (the skill dedupes by ``memory_id``).

Plus the SYNC-pair grep: the dedupe sentence appears in both skill files.

Setup rows are flushed (not committed) on ``db_session`` so they vanish on the
session rollback; the binding rows the ``AgentBindingService`` writes are
deleted at teardown. Not executed in the local unit run — needs the DB
container (``make test-integration``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
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
from services.guardrail_digest import (
    fetch_entries,
    fetch_entries_for_context,
    render_context_info_block,
    render_export_block,
)
from services.memory_service import MemoryService
from utils.datetime import utcnow
from utils.tool_trigger import guardrail_version

TT = {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge", "action": "inform"}
REPO_ROOT = Path(__file__).resolve().parents[3]


def _mem(uid, ws_id, ctx_id, *, summary, source_type=SOURCE_TYPE_MANUAL, **overrides):
    fields = {
        "id": uuid.uuid4(),
        "user_id": uid,
        "workspace_id": ws_id,
        "context_id": ctx_id,
        "summary": summary,
        "content": f"FULL CONTENT of {summary} — never in the digest",
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
    """Owner, a trusted and an external context in workspace A, a second
    workspace B the owner also belongs to, guardrail rows of every provenance,
    and one agent bound to the trusted context with ``allowed_memory_types=['note']``."""
    uid = f"e2e-digest-{uuid.uuid4().hex[:8]}"
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="Digest Owner",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.flush()

    for ws_id in (ws_a, ws_b):
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

    ctx = Context(id=uuid.uuid4(), workspace_id=ws_a, name="trusted", created_by=uid)
    ctx_ext = Context(
        id=uuid.uuid4(),
        workspace_id=ws_a,
        name="external",
        created_by=uid,
        trust_tier=CONTEXT_TRUST_TIER_EXTERNAL,
    )
    db_session.add_all([ctx, ctx_ext])
    await db_session.flush()

    high = _mem(uid, ws_a, ctx.id, summary="high note", importance=0.9, type="note")
    early = _mem(uid, ws_a, ctx.id, summary="early", importance=0.5)
    late = _mem(uid, ws_a, ctx.id, summary="late", importance=0.5)
    connector = _mem(uid, ws_a, ctx.id, summary="connector", source_type=SOURCE_TYPE_CONNECTOR)
    external = _mem(uid, ws_a, ctx_ext.id, summary="external")
    both = _mem(uid, ws_a, ctx.id, summary="both", delivery_mode=DELIVERY_MODE_ALWAYS)
    gone = _mem(uid, ws_a, ctx.id, summary="gone", deleted_at=utcnow())
    db_session.add_all([high, early, late, connector, external, both, gone])
    await db_session.flush()
    # ``created_at`` is a naive ``DateTime`` column; asyncpg binds a datetime,
    # never an ISO string (a str raises DataError at fixture setup).
    for mem, day in ((early, 1), (late, 2), (both, 3)):
        await db_session.execute(
            text("UPDATE memories SET created_at = :ts WHERE id = :id"),
            {"ts": datetime(2026, 6, day), "id": mem.id},
        )

    agent = Agent(workspace_id=ws_a, name=f"filtered-{uuid.uuid4().hex[:6]}", owner_user_id=uid)
    db_session.add(agent)
    await db_session.flush()
    from services.agent_binding_service import AgentBindingService

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
        "ws_a": ws_a,
        "ws_b": ws_b,
        "ctx": ctx,
        "ctx_ext": ctx_ext,
        "agent_id": agent.id,
        "high": high,
        "early": early,
        "late": late,
        "both": both,
        "connector": connector.id,
        "external": external.id,
        "gone": gone.id,
    }

    from models.agent import AgentContextBinding

    await db_session.execute(
        delete(AgentContextBinding).where(AgentContextBinding.agent_id == agent.id)
    )
    await db_session.commit()


# --------------------------------------------------------------------------- #
# entry source
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_trusted_manual_rows_in_repo_order_with_the_tool_triggered_version(env, db_session):
    entries = await fetch_entries_for_context(
        db_session, user_id=env["uid"], context=env["ctx"], limit=10
    )
    ids = [e.memory_id for e in entries.entries]
    assert ids == [
        str(env["high"].id),
        str(env["early"].id),
        str(env["late"].id),
        str(env["both"].id),
    ]
    assert entries.total_available == 4 and entries.truncated is False
    assert str(env["connector"]) not in ids and str(env["gone"]) not in ids
    assert all(e.authored_by_caller for e in entries.entries)
    assert all(e.source_type == SOURCE_TYPE_MANUAL for e in entries.entries)
    # The version is guardrail_version over load_guardrails.tool_triggered alone.
    resp = await MemoryService(db_session).load_guardrails(
        env["uid"], current_context_id=env["ctx"].id, current_workspace_id=env["ws_a"]
    )
    expected = guardrail_version(
        [
            [str(i.memory_id), i.summary, i.importance, i.delivery_mode, i.tool_trigger]
            for i in resp.tool_triggered
        ]
    )
    assert entries.tool_triggered_version == expected
    assert entries.tool_triggered_version != resp.version  # pinned lane is part of version


@pytest.mark.asyncio(loop_scope="session")
async def test_external_tier_and_connector_rows_yield_no_entries(env, db_session):
    entries = await fetch_entries_for_context(
        db_session, user_id=env["uid"], context=env["ctx_ext"], limit=10
    )
    assert entries.entries == [] and entries.total_available == 0
    assert entries.tool_triggered_version == "4f53cda18c2baa0c"
    assert render_export_block(entries) == ""
    assert render_context_info_block(entries)["items"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_cap_bounds_entries_and_keeps_the_true_total(env, db_session):
    entries = await fetch_entries_for_context(
        db_session, user_id=env["uid"], context=env["ctx"], limit=2
    )
    assert [e.summary for e in entries.entries] == ["high note", "early"]
    assert entries.total_available == 4 and entries.truncated is True


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_scoped_key_for_another_workspace_gets_none(env, db_session):
    denied = await fetch_entries(
        db_session,
        user_id=env["uid"],
        context_id=env["ctx"].id,
        key_workspace_id=env["ws_b"],  # a key minted for B must not reach A
        limit=5,
    )
    assert denied is None
    allowed = await fetch_entries(
        db_session,
        user_id=env["uid"],
        context_id=env["ctx"].id,
        key_workspace_id=env["ws_a"],
        limit=5,
    )
    assert allowed is not None and len(allowed.entries) == 4
    unknown = await fetch_entries(
        db_session, user_id=env["uid"], context_id=uuid.uuid4(), key_workspace_id=None, limit=5
    )
    assert unknown is None


@pytest.mark.asyncio(loop_scope="session")
async def test_binding_filter_narrows_rows_not_totals(env, db_session):
    set_agent_scope(
        AgentScope(agent_id=env["agent_id"], enforcement_mode="enforce", workspace_id=env["ws_a"])
    )
    entries = await fetch_entries_for_context(
        db_session, user_id=env["uid"], context=env["ctx"], limit=10
    )
    assert [e.summary for e in entries.entries] == ["high note"]  # type='note' only
    assert entries.total_available == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_entry_source_never_calls_the_embedding_client_or_the_vector_store(env, db_session):
    boom = AsyncMock(side_effect=AssertionError("called on the guardrail digest lane"))
    with (
        patch("db.qdrant.search_memories_qdrant", new=boom),
        patch("services.embedding_service.EmbeddingService.embed", new=boom),
        patch("services.embedding_service.EmbeddingService.embed_with_usage", new=boom),
    ):
        entries = await fetch_entries(
            db_session, user_id=env["uid"], context_id=env["ctx"].id, key_workspace_id=None, limit=5
        )
    assert entries is not None and len(entries.entries) == 4
    boom.assert_not_awaited()


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_deny_at_the_resolver_writes_one_denied_row_and_a_success_writes_none(
    env, db_session
):
    """The digest bypasses ``load_guardrails`` (no success audit row per
    connect) but keeps the resolver's deny capture: an agent whose bindings
    exclude the requested context gets ``None`` and exactly one
    ``outcome="denied"`` emission under ``operation="load_guardrails"``."""
    set_agent_scope(
        AgentScope(agent_id=env["agent_id"], enforcement_mode="enforce", workspace_id=env["ws_a"])
    )
    emit = AsyncMock()
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=emit):
        denied = await fetch_entries(
            db_session,
            user_id=env["uid"],
            context_id=env["ctx_ext"].id,  # bound to ``ctx`` only → binding deny
            key_workspace_id=None,
            limit=5,
        )
        assert denied is None
        denied_calls = [c for c in emit.await_args_list if c.kwargs.get("outcome") == "denied"]
        assert len(denied_calls) == 1
        assert denied_calls[0].kwargs["operation"] == "load_guardrails"

        emit.reset_mock()
        served = await fetch_entries(
            db_session, user_id=env["uid"], context_id=env["ctx"].id, key_workspace_id=None, limit=5
        )
    assert served is not None and [e.summary for e in served.entries] == ["high note"]
    assert not [c for c in emit.await_args_list if c.kwargs.get("outcome") == "success"]


# --------------------------------------------------------------------------- #
# get_context_info block
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_get_context_info_block_lists_a_memory_that_is_both_pinned_and_tool_triggered(
    env, db_session, monkeypatch
):
    from mcp_server.tools.context import handle_get_context_info

    async def _get_db():
        yield db_session

    monkeypatch.setattr(db_session, "commit", AsyncMock())  # keep the fixture rows uncommitted
    with (
        patch("db.base.get_db", new=_get_db),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
    ):
        result = await handle_get_context_info(
            {"context_id": str(env["ctx"].id)}, env["uid"], env["ws_a"]
        )
    payload = json.loads(result[0].text)
    assert payload["status"] == "success", payload
    block = payload["guardrails"]
    ids = [i["memory_id"] for i in block["items"]]
    assert str(env["both"].id) in ids  # pinned AND tool-triggered → present here too
    assert ids == [
        str(env["high"].id),
        str(env["early"].id),
        str(env["late"].id),
        str(env["both"].id),
    ]
    assert block["total_available"] == 4 and block["truncated"] is False
    assert "version" not in block and len(block["tool_triggered_version"]) == 16
    # The pinned read lists it as well: the skill dedupes by memory_id.
    pinned = await MemoryService(db_session).load_pinned(
        env["uid"], current_context_id=env["ctx"].id, current_workspace_id=env["ws_a"]
    )
    assert str(env["both"].id) in {str(m.memory_id) for m in pinned.memories}


# --------------------------------------------------------------------------- #
# skills: the SYNC pair carries the dedupe rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative",
    [
        "claude-skills/session-start.md",
        "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
    ],
)
def test_skill_text_dedupes_guardrails_against_load_pinned_by_memory_id(relative):
    text_ = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert "skipping any `memory_id` already shown" in text_
    assert "guardrails.items" in text_
    assert "if it is `null` the read failed" in text_
    assert "hook is installed" not in text_  # the URL switch decides, not the model
