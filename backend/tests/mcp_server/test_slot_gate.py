"""The workspace slot gate on every MCP tool (#2918), on real rows.

One owner owns two workspaces: S, slot-suspended, and K, active. Every tool of
the capacity tables (``_capacity_gate``: the whole registry) is called

* with no target, on a suspended session workspace: refused with
  ``workspace_suspended`` (the manuals excepted), and never in mode ``off`` or
  ``shadow`` although the suspension row exists;
* naming a context of the active workspace from the suspended session: never
  ``workspace_suspended`` (target-based, as #1954);
* naming a context of the suspended workspace from the active session:
  ``workspace_suspended``.

Embedding, vector store, blob storage and quota calls are stubbed; every
session is on the test database (``db.base.get_db`` is pointed at it).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.agent_scope import set_agent_scope
from auth.workspace_roles import WorkspaceRole
from mcp_server.tools._capacity_gate import (
    CAPACITY_LOCK_ALLOWED_TOOLS,
    CAPACITY_LOCK_BLOCKED_TOOLS,
    invalidate_capacity_candidate_cache,
)
from mcp_server.tools._helpers import _mcp_key_workspace_scope
from mcp_server.tools._slot_gate import SLOT_GATE_EXEMPT_TOOLS
from models.agent import Agent
from models.auth import Context, User, Workspace, WorkspaceMember
from models.file_objects import FileObject
from models.memory import Memory, NeuralMemoryEdge
from models.workspace_slots import WorkspaceSlotSuspension

pytestmark = pytest.mark.asyncio(loop_scope="session")

ALL_TOOLS = sorted(CAPACITY_LOCK_BLOCKED_TOOLS | CAPACITY_LOCK_ALLOWED_TOOLS)
GATED_TOOLS = [t for t in ALL_TOOLS if t not in SLOT_GATE_EXEMPT_TOOLS]
T0 = datetime(2031, 5, 1, 12, 0, 0)


@dataclass
class World:
    session: AsyncSession
    owner: str
    ws: dict[str, UUID] = field(default_factory=dict)
    ctx: dict[str, UUID] = field(default_factory=dict)
    mem: dict[str, UUID] = field(default_factory=dict)
    files: dict[str, UUID] = field(default_factory=dict)
    agents: dict[str, UUID] = field(default_factory=dict)


def _mode(mode: str):
    return patch("services.workspace_slots.workspace_slot_mode", return_value=mode)


def _memory(ws: UUID, ctx: UUID, author: str, summary: str) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=ws,
        context_id=ctx,
        user_id=author,
        summary=summary,
        content="slot gate fixture",
        type="decision",
        scope="persistent",
        client="test",
        importance=0.5,
        embedding_status="success",
        tags=["slot"],
    )


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[World]:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    invalidate_capacity_candidate_cache()
    tag = uuid4().hex[:8]
    owner = f"slot-owner-{tag}"
    async with factory() as s:
        s.add(User(email=f"{owner}@example.test", user_id=owner, name=owner, role="user"))
        await s.flush()
        ws = {}
        for key in ("s", "k"):
            w = Workspace(
                id=uuid4(),
                name=f"slot-{key}-{tag}",
                # Pro granted by an admin: no billing contract, so a
                # suspension row counts (the read-time rule).
                plan_name="pro",
                entitlement_source="admin_grant",
                owner_user_id=owner,
                daily_api_limit=50000,
                weekly_api_limit=250000,
            )
            s.add(w)
            ws[key] = w.id
        await s.flush()
        s.add_all(
            [
                WorkspaceMember(workspace_id=wid, user_id=owner, role=WorkspaceRole.OWNER)
                for wid in ws.values()
            ]
        )
        ctx: dict[str, UUID] = {}
        for key in ("s", "s2", "k", "k2"):
            c = Context(
                id=uuid4(),
                workspace_id=ws[key[0]],
                name=f"slot-{key}-{tag}",
                created_by=owner,
                is_private=False,
            )
            s.add(c)
            ctx[key] = c.id
        await s.flush()
        mems = {
            "s": _memory(ws["s"], ctx["s"], owner, "S note"),
            "s_b": _memory(ws["s"], ctx["s"], owner, "S second note"),
            "k": _memory(ws["k"], ctx["k"], owner, "K note"),
            "k_b": _memory(ws["k"], ctx["k"], owner, "K second note"),
        }
        s.add_all(list(mems.values()))
        files = {}
        for key in ("s", "k"):
            f = FileObject(
                id=uuid4(),
                workspace_id=ws[key],
                context_id=ctx[key],
                sha256=(key * 64)[:64],
                size_bytes=10,
                content_type="text/plain",
                filename=f"{key}.txt",
                storage_key=f"{ws[key]}/{key}{key}/{(key * 64)[:64]}",
                status="uploaded",
                created_by=owner,
            )
            s.add(f)
            files[key] = f.id
        agents = {}
        for key in ("s", "k"):
            a = Agent(workspace_id=ws[key], name=f"slot-agent-{key}-{tag}", owner_user_id=owner)
            s.add(a)
            await s.flush()
            agents[key] = a.id
        s.add(
            WorkspaceSlotSuspension(
                workspace_id=ws["s"], owner_user_id=owner, cause="billing_snapshot", suspended_at=T0
            )
        )
        await s.commit()
        w = World(
            session=s,
            owner=owner,
            ws=ws,
            ctx=ctx,
            mem={k: v.id for k, v in mems.items()},
            files=files,
            agents=agents,
        )
        try:
            yield w
        finally:
            set_agent_scope(None)
            await s.rollback()
            ids = list(ws.values())
            for stmt in (
                delete(WorkspaceSlotSuspension).where(
                    WorkspaceSlotSuspension.workspace_id.in_(ids)
                ),
                delete(NeuralMemoryEdge).where(NeuralMemoryEdge.workspace_id.in_(ids)),
                delete(FileObject).where(FileObject.workspace_id.in_(ids)),
                delete(Agent).where(Agent.workspace_id.in_(ids)),
                delete(Workspace).where(Workspace.id.in_(ids)),
                delete(User).where(User.user_id == owner),
            ):
                await s.execute(stmt)
                await s.commit()
            invalidate_capacity_candidate_cache()


@pytest.fixture
def live(world: World) -> Any:
    async def _db() -> AsyncIterator[AsyncSession]:
        yield world.session

    from services.memory_service import MemoryService

    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    storage = MagicMock(
        generate_presigned_get=AsyncMock(return_value="https://example.test/get"),
        generate_presigned_put=AsyncMock(return_value="https://example.test/put"),
    )
    with (
        patch("db.base.get_db", new=_db),
        # Bound at import time: point it at the same session too.
        patch("mcp_server.tools.secrets.get_db", new=_db),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch(
            "services.embedding_service.EmbeddingService",
            return_value=MagicMock(embed=AsyncMock(return_value=[0.1])),
        ),
        patch("services.quota_service.QuotaService", return_value=quota),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()),
        patch("db.qdrant.delete_points_from_qdrant", new=AsyncMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch.object(MemoryService, "_delete_memory_point", new=AsyncMock()),
        patch.object(MemoryService, "_point_to_delete", new=AsyncMock(return_value=None)),
        patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))),
        patch("services.file_storage_service.get_blob_storage", return_value=storage),
        patch(
            "services.file_storage_service.storage_quota_service.release_storage_bytes",
            new=AsyncMock(),
        ),
        patch(
            "services.file_storage_service.storage_quota_service.reserve_storage_bytes",
            new=AsyncMock(),
        ),
    ):
        yield world


async def _tool(tool: str, args: dict[str, Any], user_id: str, session_ws: UUID) -> dict:
    from mcp_server.tools import execute_tool_call

    token = _mcp_key_workspace_scope.set(None)
    try:
        result = await execute_tool_call(tool, args, user_id, session_ws)
    finally:
        _mcp_key_workspace_scope.reset(token)
        await _reset(session_ws)
    text = result[0].text
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return body if isinstance(body, dict) else {"body": body}


async def _reset(_ws: UUID) -> None:
    """Nothing: the session is shared; a handler commits or rolls back itself."""


def _target_args(w: World, side: str) -> dict[str, dict[str, Any]]:
    """Minimal arguments that name a target of workspace ``side`` ("s" or "k")."""
    c, c2 = str(w.ctx[side]), str(w.ctx[f"{side}2"])
    m, m2 = str(w.mem[side]), str(w.mem[f"{side}_b"])
    f, agent = str(w.files[side]), str(w.agents[side])
    wid = str(w.ws[side])
    return {
        "analyze_context": {"context_id": c, "dry_run": True},
        "bootstrap": {"context_id": c},
        "changes_since": {"context_id": c, "since": "2020-01-01T00:00:00Z"},
        "create_edge": {"context_id": c, "source_id": m, "target_id": m2},
        "delete_context": {"context_id": c2},
        "delete_edge": {"context_id": c, "source_id": m, "target_id": m2},
        "explore": {"context_id": c, "memory_id": m},
        "feedback": {"context_id": c, "memory_id": m, "helpful": True},
        "forget": {"context_id": c, "memory_id": m2},
        "get_active_analysis": {"context_id": c},
        "get_context_info": {"context_id": c},
        "get_sleep_history": {"context_id": c},
        "get_state": {"context_id": c},
        "list": {"context_id": c},
        "list_analyses": {"context_id": c},
        "list_edges": {"context_id": c, "memory_id": m},
        "list_revisions": {"context_id": c, "memory_id": m},
        "list_tags": {"context_id": c},
        "load_guardrails": {"context_id": c},
        "load_pinned": {"context_id": c},
        "merge_contexts": {"source_context_id": c2, "target_context_id": c},
        "recall": {"context_id": c, "query": "note"},
        "recall_nearby": {"context_id": c, "lat": 35.0, "lon": 139.0},
        "recall_series": {"context_id": c, "metric": "weight"},
        "recall_upcoming": {"context_id": c},
        "record_measurement": {"context_id": c, "metric": "weight", "value": 1},
        "reference": {"context_id": c, "memory_id": m},
        "remember": {
            "context_id": c,
            "summary": "a slot gate note",
            "content": "body",
            "type": "decision",
        },
        "remember_batch": {
            "context_id": c,
            "items": [{"summary": "a slot gate note", "content": "body", "type": "decision"}],
        },
        "rollback_session": {"context_id": c, "dry_run": True, "session_id": "s-1"},
        "set_state": {"context_id": c, "key": "k", "value": {"a": 1}},
        "update_context": {"context_id": c, "description": "changed"},
        "update_edge": {"context_id": c, "source_id": m, "target_id": m2, "weight": 0.5},
        "update_memory": {"context_id": c, "memory_id": m, "importance": 0.4},
        "update_search_config": {"context_id": c, "semantic_weight": 0.6},
        "list_files": {"workspace_id": wid},
        "get_file_download_url": {"file_id": f, "workspace_id": wid},
        "delete_file": {"file_id": f, "workspace_id": wid},
        "init_file_upload": {
            "workspace_id": wid,
            "context_id": c,
            "filename": "x.txt",
            "content_type": "text/plain",
            "size_bytes": 1,
            "sha256": "c" * 64,
        },
        "complete_file_upload": {"file_id": f, "workspace_id": wid, "sha256": "a" * 64},
        "get_agent_bootstrap": {"agent_id": agent, "context_id": c},
        "describe_binding": {"context_id": c},
    }


def _error(body: dict) -> str | None:
    return body.get("error")


@pytest.mark.parametrize("tool", GATED_TOOLS)
async def test_a_target_less_call_on_a_suspended_session_is_refused(live, tool) -> None:
    w: World = live
    with _mode("enforce"):
        body = await _tool(tool, {}, w.owner, w.ws["s"])
    assert _error(body) == "workspace_suspended", body
    # The owner is told where to resume it.
    assert body.get("workspace_id") == str(w.ws["s"]), body
    assert str(body.get("manage_url", "")).endswith("/profile/workspaces"), body


@pytest.mark.parametrize("tool", sorted(SLOT_GATE_EXEMPT_TOOLS))
async def test_the_manuals_answer_on_a_suspended_session(live, tool) -> None:
    w: World = live
    with _mode("enforce"):
        body = await _tool(tool, {}, w.owner, w.ws["s"])
    assert _error(body) != "workspace_suspended", body


@pytest.mark.parametrize("mode", ["off", "shadow"])
@pytest.mark.parametrize("tool", GATED_TOOLS)
async def test_no_refusal_unless_enforced(live, tool, mode) -> None:
    w: World = live
    args = _target_args(w, "s").get(tool, {})
    with _mode(mode):
        target_less = await _tool(tool, {}, w.owner, w.ws["s"])
        named = await _tool(tool, args, w.owner, w.ws["k"])
    assert _error(target_less) != "workspace_suspended", target_less
    assert _error(named) != "workspace_suspended", named


TARGETED = sorted(
    {
        "analyze_context",
        "bootstrap",
        "changes_since",
        "create_edge",
        "delete_context",
        "delete_edge",
        "explore",
        "feedback",
        "forget",
        "get_active_analysis",
        "get_context_info",
        "get_sleep_history",
        "get_state",
        "list",
        "list_analyses",
        "list_edges",
        "list_revisions",
        "list_tags",
        "load_guardrails",
        "load_pinned",
        "merge_contexts",
        "recall",
        "recall_nearby",
        "recall_series",
        "recall_upcoming",
        "record_measurement",
        "reference",
        "remember",
        "remember_batch",
        "rollback_session",
        "set_state",
        "update_context",
        "update_edge",
        "update_memory",
        "update_search_config",
        "list_files",
        "get_file_download_url",
        "delete_file",
        "init_file_upload",
        "complete_file_upload",
        "get_agent_bootstrap",
    }
)


@pytest.mark.parametrize("tool", TARGETED)
async def test_a_target_in_an_active_workspace_works_from_a_suspended_session(live, tool) -> None:
    w: World = live
    with _mode("enforce"):
        body = await _tool(tool, _target_args(w, "k")[tool], w.owner, w.ws["s"])
    assert _error(body) != "workspace_suspended", body


# Tools that work inside the session workspace only (the analysis tools check
# the context against it; an agent is looked up there): from another session
# a target is simply not found. Named targets skip the dispatcher's check, so
# the handler's own gate answers from the suspended session itself.
SESSION_SCOPED = {"analyze_context", "get_active_analysis", "list_analyses", "get_agent_bootstrap"}


@pytest.mark.parametrize("tool", sorted(set(TARGETED) - SESSION_SCOPED))
async def test_a_target_in_a_suspended_workspace_is_refused_from_an_active_session(
    live, tool
) -> None:
    w: World = live
    with _mode("enforce"):
        body = await _tool(tool, _target_args(w, "s")[tool], w.owner, w.ws["k"])
    assert _error(body) == "workspace_suspended", body


@pytest.mark.parametrize("tool", sorted(SESSION_SCOPED))
async def test_a_session_scoped_tool_naming_its_target_is_refused_by_its_handler(
    live, tool
) -> None:
    w: World = live
    with _mode("enforce"):
        body = await _tool(tool, _target_args(w, "s")[tool], w.owner, w.ws["s"])
    assert _error(body) == "workspace_suspended", body


# ---------------------------------------------------------------------------
# The write resolver: confinement before the gate
# ---------------------------------------------------------------------------


async def test_a_key_of_an_active_workspace_naming_a_suspended_context_gets_not_found(
    live,
) -> None:
    """A key scoped to K is confined to K before the gate looks at S: the
    caller gets the uniform not-found, never S's suspension."""
    from mcp_server.tools import execute_tool_call

    w: World = live
    args = _target_args(w, "s")["remember"]
    token = _mcp_key_workspace_scope.set(w.ws["k"])
    try:
        with _mode("enforce"):
            result = await execute_tool_call("remember", args, w.owner, w.ws["k"])
    finally:
        _mcp_key_workspace_scope.reset(token)
    body = json.loads(result[0].text)
    assert _error(body) == "context_not_found", body


async def test_an_agent_binding_hiding_a_suspended_context_gets_not_found(live) -> None:
    """An enforce-mode agent key whose binding does not name the context gets
    the uniform not-found, also when the context's workspace is suspended."""
    from auth.agent_scope import AgentScope

    w: World = live
    scope = AgentScope(agent_id=w.agents["s"], enforcement_mode="enforce", workspace_id=w.ws["s"])
    set_agent_scope(scope)
    try:
        with _mode("enforce"):
            body = await _tool("remember", _target_args(w, "s")["remember"], w.owner, w.ws["s"])
    finally:
        set_agent_scope(None)
    assert _error(body) == "context_not_found", body
