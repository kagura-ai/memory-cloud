"""End-to-end validation of the #1293 trusted-tier gate on the non-recall
behaviour-establishing bootstrap lanes (pinned + upcoming time memories).

The recall lane already drops external/connector-origin rows when bootstrap
passes ``filters={"trust_tier": "trusted"}`` (agent_bootstrap_service `_recall`).
Before #1293 the **pinned** lane (`load_pinned` → `MemoryRepository.list_pinned`)
and the **time-memory** lane (`query_upcoming_time_memories`) applied no trust
filter, so a connector-origin (e.g. Slack/Discord-ingested) pinned or upcoming
memory surfaced verbatim at session start — an indirect prompt-injection vector
on exactly the behaviour-establishing surface the recall lane guards
(OWASP LLM01/LLM03, F2 invariant 3).

These tests pin the fix at the query layer against the real DB: the same
two-part gate recall uses (context ``trust_tier == trusted`` + row-level
``source_type != connector``) now applies when ``trusted_only=True``, and stays
a no-op (returns everything) when it is not — the standalone ``load_pinned`` /
``recall_upcoming`` tools are user-initiated reads and keep their prior default.

Setup rows are flushed (not committed) on ``db_session`` so they vanish on the
session rollback. ``load_pinned``'s ``memory_access_events`` writer is a no-op
here (no verified agent identity is set), so there is no committed side effect
to clean up.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from auth.workspace_roles import WorkspaceRole
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
from services.memory_service import MemoryService
from services.time_memory import query_upcoming_time_memories


def _mem(uid, ws_id, ctx_id, *, summary, source_type, pinned=False, time=False):
    # trigger_from/trigger_until are STORED generated columns
    # (details->'trigger'->>'from'/'until') — never set them directly; seed the
    # ``details.trigger`` JSON and Postgres computes them.
    return Memory(
        id=uuid.uuid4(),
        user_id=uid,
        workspace_id=ws_id,
        context_id=ctx_id,
        summary=summary,
        content=f"body::{summary}",
        type="time" if time else "note",
        client="test",
        tags=[],
        source_type=source_type,
        delivery_mode=DELIVERY_MODE_ALWAYS if pinned else "on_recall",
        details=(
            {"trigger": {"from": "2026-01-01T00:00:00", "until": "2099-12-31T23:59:59"}}
            if time
            else None
        ),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def env(db_session):
    """Workspace + owner + a trusted context (default) and an external context,
    each seeded with a manual and a connector-origin pinned + time memory."""
    uid = f"e2e-trust-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()

    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="E2E Trust Owner",
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

    # Trusted context (server_default trust_tier='trusted') + an external one.
    ctx_trusted = Context(id=uuid.uuid4(), workspace_id=ws_id, name="trusted", created_by=uid)
    ctx_external = Context(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        name="external",
        created_by=uid,
        trust_tier=CONTEXT_TRUST_TIER_EXTERNAL,
    )
    db_session.add_all([ctx_trusted, ctx_external])
    await db_session.flush()

    p_manual = _mem(
        uid,
        ws_id,
        ctx_trusted.id,
        summary="pin-manual",
        source_type=SOURCE_TYPE_MANUAL,
        pinned=True,
    )
    p_conn = _mem(
        uid,
        ws_id,
        ctx_trusted.id,
        summary="pin-connector",
        source_type=SOURCE_TYPE_CONNECTOR,
        pinned=True,
    )
    p_ext = _mem(
        uid,
        ws_id,
        ctx_external.id,
        summary="pin-external",
        source_type=SOURCE_TYPE_MANUAL,
        pinned=True,
    )
    t_manual = _mem(
        uid, ws_id, ctx_trusted.id, summary="time-manual", source_type=SOURCE_TYPE_MANUAL, time=True
    )
    t_conn = _mem(
        uid,
        ws_id,
        ctx_trusted.id,
        summary="time-connector",
        source_type=SOURCE_TYPE_CONNECTOR,
        time=True,
    )
    t_ext = _mem(
        uid,
        ws_id,
        ctx_external.id,
        summary="time-external",
        source_type=SOURCE_TYPE_MANUAL,
        time=True,
    )
    db_session.add_all([p_manual, p_conn, p_ext, t_manual, t_conn, t_ext])
    await db_session.flush()

    yield {
        "uid": uid,
        "ws_id": ws_id,
        "ctx_trusted": ctx_trusted.id,
        "ctx_external": ctx_external.id,
        "p_manual": p_manual.id,
        "p_conn": p_conn.id,
        "t_manual": t_manual.id,
        "t_conn": t_conn.id,
    }


# --------------------------------------------------------------------------- #
# pinned lane (load_pinned -> list_pinned)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_pinned_trusted_only_drops_connector_row(env, db_session):
    # #1293: the bootstrap pinned lane must not surface a connector-origin
    # pinned memory even inside an otherwise-trusted context (row-level
    # defense-in-depth, mirrors recall).
    resp = await MemoryService(db_session).load_pinned(
        user_id=env["uid"],
        current_context_id=env["ctx_trusted"],
        current_workspace_id=env["ws_id"],
        trusted_only=True,
    )
    returned = {str(m.memory_id) for m in resp.memories}
    assert str(env["p_manual"]) in returned
    assert str(env["p_conn"]) not in returned


@pytest.mark.asyncio(loop_scope="session")
async def test_pinned_default_is_unfiltered(env, db_session):
    # Backward-compat: the standalone load_pinned surface (trusted_only=False)
    # is a user-initiated read and must still return every pinned row.
    resp = await MemoryService(db_session).load_pinned(
        user_id=env["uid"],
        current_context_id=env["ctx_trusted"],
        current_workspace_id=env["ws_id"],
    )
    returned = {str(m.memory_id) for m in resp.memories}
    assert str(env["p_manual"]) in returned
    assert str(env["p_conn"]) in returned


@pytest.mark.asyncio(loop_scope="session")
async def test_pinned_trusted_only_external_context_returns_nothing(env, db_session):
    # An external-tier context is untrusted wholesale — nothing from it may
    # establish behaviour at session start.
    resp = await MemoryService(db_session).load_pinned(
        user_id=env["uid"],
        current_context_id=env["ctx_external"],
        current_workspace_id=env["ws_id"],
        trusted_only=True,
    )
    assert resp.memories == []


# --------------------------------------------------------------------------- #
# upcoming lane (query_upcoming_time_memories)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_upcoming_trusted_only_drops_connector_row(env, db_session):
    rows = await query_upcoming_time_memories(
        db_session, env["ctx_trusted"], q_from=None, q_until=None, k=20, trusted_only=True
    )
    ids = {r["memory_id"] for r in rows}
    assert str(env["t_manual"]) in ids
    assert str(env["t_conn"]) not in ids


@pytest.mark.asyncio(loop_scope="session")
async def test_upcoming_trusted_only_external_context_returns_nothing(env, db_session):
    rows = await query_upcoming_time_memories(
        db_session, env["ctx_external"], q_from=None, q_until=None, k=20, trusted_only=True
    )
    assert rows == []


@pytest.mark.asyncio(loop_scope="session")
async def test_upcoming_default_is_unfiltered(env, db_session):
    rows = await query_upcoming_time_memories(
        db_session, env["ctx_trusted"], q_from=None, q_until=None, k=20
    )
    ids = {r["memory_id"] for r in rows}
    assert str(env["t_manual"]) in ids
    assert str(env["t_conn"]) in ids
