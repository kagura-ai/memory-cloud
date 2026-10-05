"""``SessionBootstrapService`` composes the session-start lanes on real Postgres (#1851)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from services.memory_listing import changes_since, decode_change_cursor
from services.session_bootstrap_service import (
    COMPONENTS,
    SessionBootstrapService,
    window_start_cursor,
)
from utils.datetime import utcnow
from utils.response_budget import json_chars


async def _scope(db, owner: str, *, private: bool = False) -> tuple[Workspace, Context]:
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"sb-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    # load_pinned reads through ContextService.get_context: membership is required.
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"sb-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=private,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return ws, ctx


async def _row(db, user_id: str, ws: UUID, ctx: UUID, **over) -> Memory:
    base = {
        "id": uuid4(),
        "user_id": user_id,
        "workspace_id": ws,
        "context_id": ctx,
        "summary": over.pop("summary", "row"),
        "content": "content",
        "type": "note",
        "importance": 0.5,
        "confidence": 1.0,
        "client": "pytest",
        "source": "manual",
        "source_type": "manual",
        "created_at": utcnow(),
        "embedding_status": "success",
    }
    base.update(over)
    mem = Memory(**base)
    db.add(mem)
    await db.flush()
    return mem


@pytest.fixture
async def seeded(db_session):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    now = utcnow()
    next_month = now + timedelta(days=40)
    rows = {
        "pinned": await _row(
            db_session, owner, ws.id, ctx.id, summary="standing rule", delivery_mode="always"
        ),
        "due": await _row(
            db_session,
            owner,
            ws.id,
            ctx.id,
            summary="renew the certificate",
            type="time",
            # Normalised the way remember() stores it (MemoryService._apply_time_trigger).
            details={
                "trigger": {
                    "year": next_month.year,
                    "month": next_month.month,
                    "from": next_month.strftime("%Y-%m-01T00:00:00"),
                    "until": next_month.strftime("%Y-%m-28T23:59:59"),
                }
            },
        ),
        "recent": await _row(
            db_session,
            owner,
            ws.id,
            ctx.id,
            summary="yesterday's decision",
            created_at=now - timedelta(days=1),
        ),
        "old": await _row(
            db_session,
            owner,
            ws.id,
            ctx.id,
            summary="last month's note",
            created_at=now - timedelta(days=30),
        ),
    }
    await db_session.commit()
    await db_session.refresh(ctx)
    return owner, ws, ctx, rows


async def _build(db, owner, ws, ctx, **kw):
    kw.setdefault("since", utcnow() - timedelta(days=7))
    kw.setdefault("max_chars", 20_000)
    return await SessionBootstrapService(db).build(
        user_id=owner, workspace_id=ws.id, context=ctx, **kw
    )


@pytest.mark.asyncio
async def test_all_lanes_return_in_one_envelope(db_session, seeded):
    owner, ws, ctx, rows = seeded

    async def provider():
        return {"guardrails": {"items": []}}

    env = await _build(db_session, owner, ws, ctx, guardrails_provider=provider)

    assert env["status"] == "success" and env["degraded"] is False
    assert env["context"]["id"] == str(ctx.id)
    assert env["guardrails"] == {"items": []}  # the handler's block passes through unchanged
    assert set(env["components"]) == set(COMPONENTS)
    pinned = env["components"]["pinned"]
    assert pinned["status"] == "ok"
    assert [m["memory_id"] for m in pinned["memories"]] == [str(rows["pinned"].id)]
    assert pinned["cap"] == 20 and pinned["truncated"] is False
    upcoming = env["components"]["upcoming"]
    assert upcoming["status"] == "ok"
    assert [r["memory_id"] for r in upcoming["results"]] == [str(rows["due"].id)]
    changes = env["components"]["changes"]
    assert changes["status"] == "ok" and changes["has_more"] is False
    created = {c["memory_id"] for c in changes["changes"] if c["kind"] == "created"}
    assert str(rows["recent"].id) in created and str(rows["pinned"].id) in created
    assert str(rows["old"].id) not in {c["memory_id"] for c in changes["changes"]}
    assert env["since"].endswith("Z") and env["generated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_include_narrows_the_components(db_session, seeded):
    owner, ws, ctx, _ = seeded
    env = await _build(db_session, owner, ws, ctx, include=("changes",))
    assert set(env["components"]) == {"changes"}
    assert "guardrails" not in env  # none passed: the key is simply absent


@pytest.mark.asyncio
async def test_a_failing_lane_degrades_without_sinking_the_others(db_session, seeded, monkeypatch):
    owner, ws, ctx, rows = seeded
    pinned_id = str(rows["pinned"].id)  # read before the lane's rollback expires the row

    async def boom(self, context_id):
        raise RuntimeError("time index down")

    monkeypatch.setattr(SessionBootstrapService, "_upcoming", boom)
    env = await _build(db_session, owner, ws, ctx)
    assert env["degraded"] is True
    assert env["components"]["upcoming"] == {"status": "error", "error": "component_failed"}
    assert env["components"]["pinned"]["status"] == "ok"
    assert env["components"]["changes"]["status"] == "ok"
    assert [m["memory_id"] for m in env["components"]["pinned"]["memories"]] == [pinned_id]


@pytest.mark.asyncio
async def test_a_small_budget_truncates_and_says_so(db_session, seeded):
    owner, ws, ctx, _ = seeded
    for i in range(12):
        await _row(db_session, owner, ws.id, ctx.id, summary=f"filler {i} " + "x" * 300)
    await db_session.commit()
    full = await _build(db_session, owner, ws, ctx)
    # The context block and instructions are never cut: the budget must leave
    # room for them plus a few items.
    shell = dict(full, components={})
    budget = json_chars(shell) + 1_200
    env = await _build(db_session, owner, ws, ctx, max_chars=budget)
    assert json_chars(env) <= budget < json_chars(full)
    changes = env["components"]["changes"]
    assert changes["truncated"] is True and changes["has_more"] is True
    assert len(changes["changes"]) < len(full["components"]["changes"]["changes"])
    assert env["components"]["pinned"]["memories"]  # the pinned lane is cut last
    assert env["context"]["id"] == str(ctx.id)
    # Paging on from the cut page reproduces the rest: nothing skipped, nothing twice.
    rest = await changes_since(
        db_session,
        context_id=ctx.id,
        owner_user_id=None,
        since=utcnow() - timedelta(days=7),
        until=None,
        cursor=changes["next_cursor"],
        limit=50,
    )
    seen = [c["memory_id"] for c in changes["changes"]] + [str(c.memory_id) for c in rest.changes]
    assert seen == [c["memory_id"] for c in full["components"]["changes"]["changes"]]


@pytest.mark.asyncio
async def test_a_failing_guardrails_read_rolls_back_without_sinking_the_lanes(db_session, seeded):
    """The guardrails block is read between the context block and the lanes; its
    fail-open rollback (as ``_guardrails_field`` does) must leave the lanes and
    the caller's Context usable."""
    owner, ws, ctx, rows = seeded
    pinned_id = str(rows["pinned"].id)

    async def failing_provider():
        try:
            await db_session.execute(text("SELECT * FROM no_such_table_1851"))
        except Exception:
            await db_session.rollback()  # what the fail-open read does
            return {"guardrails": None}
        return {}

    env = await _build(db_session, owner, ws, ctx, guardrails_provider=failing_provider)
    assert env["guardrails"] is None
    assert env["degraded"] is False
    assert [m["memory_id"] for m in env["components"]["pinned"]["memories"]] == [pinned_id]
    assert env["context"]["id"] == str(ctx.id)


def test_window_start_cursor_sorts_before_every_event_in_the_window():
    since = datetime(2026, 10, 1, 12, 0)
    at, kind, mid = decode_change_cursor(window_start_cursor(since))
    assert (at, kind, mid) == (since, "created", UUID(int=0))


@pytest.mark.asyncio
async def test_a_database_error_in_one_lane_does_not_poison_the_session(db_session, seeded):
    """A failed statement leaves the session in a failed transaction; the lane's
    rollback must let the later lanes run (the agent bootstrap's boundary)."""
    owner, ws, ctx, rows = seeded
    recent_id = str(rows["recent"].id)  # read before the rollback expires the row
    ctx_id = ctx.id

    async def bad_sql(self, user_id, workspace_id, context_id):
        await self.db.execute(text("SELECT * FROM no_such_table_1851"))
        return {}

    with patch.object(SessionBootstrapService, "_pinned", bad_sql):
        env = await _build(db_session, owner, ws, ctx)
    assert env["degraded"] is True
    assert env["components"]["pinned"]["status"] == "error"
    assert env["components"]["upcoming"]["status"] == "ok"
    assert env["components"]["changes"]["status"] == "ok"
    assert recent_id in {c["memory_id"] for c in env["components"]["changes"]["changes"]}
    # The rollback expired the caller's Context; the handler reads its context
    # fields before build() for exactly this reason, and the envelope's block
    # was read before the lanes ran.
    assert env["context"]["id"] == str(ctx_id)


@pytest.mark.asyncio
async def test_private_context_lists_only_the_owners_changes(db_session):
    owner, other = f"o-{uuid4().hex[:6]}", f"x-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner, private=True)
    mine = await _row(db_session, owner, ws.id, ctx.id, summary="mine")
    await _row(db_session, other, ws.id, ctx.id, summary="theirs")
    await db_session.commit()
    await db_session.refresh(ctx)
    env = await _build(db_session, owner, ws, ctx, include=("changes",))
    assert [c["memory_id"] for c in env["components"]["changes"]["changes"]] == [str(mine.id)]


@pytest.mark.asyncio
async def test_changes_component_withholds_the_summary_of_a_forgotten_memory(db_session, seeded):
    """#1876: bootstrap reads the same log as ``changes_since`` — a forgotten
    memory's events are listed with their kind and time, without the summary."""
    owner, ws, ctx, rows = seeded
    now = utcnow()
    gone = await _row(
        db_session,
        owner,
        ws.id,
        ctx.id,
        summary="stored by mistake",
        created_at=now - timedelta(days=2),
        deleted_at=now - timedelta(days=1, hours=23),
    )
    await db_session.commit()
    await db_session.refresh(ctx)
    env = await _build(db_session, owner, ws, ctx, include=("changes",))
    changes = env["components"]["changes"]["changes"]
    events = [c for c in changes if c["memory_id"] == str(gone.id)]
    assert [c["kind"] for c in events] == ["created", "forgotten"]
    assert all(set(c) == {"memory_id", "kind", "at"} for c in events)
    assert "stored by mistake" not in str(env)
    recent = next(c for c in changes if c["memory_id"] == str(rows["recent"].id))
    assert recent["summary"] == "yesterday's decision"  # live rows keep theirs
