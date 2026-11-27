"""#3019 (points 3 and 4), on Postgres: Sleep coverage and the re-evaluation clock.

Rows are dated explicitly; ``now`` is taken inside each test. No test sleeps.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from services.memory_listing import changes_since
from services.sleep.importance_reeval import STALENESS_DAYS, ImportanceReevalPhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


async def _scope(db) -> tuple[str, Workspace, Context]:
    owner = f"cov_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"cov-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"cov-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return owner, ws, ctx


def _memory(owner: str, ws: Workspace, ctx: Context, **fields) -> Memory:
    values = {
        "summary": f"memory {uuid4().hex[:8]}",
        "content": "c",
        "type": "note",
        "importance": 0.5,
        "client": "pytest",
        "scope": "working",
        "embedding_status": "success",
        **fields,
    }
    return Memory(id=uuid4(), workspace_id=ws.id, context_id=ctx.id, user_id=owner, **values)


def _reeval_config():
    from unittest.mock import MagicMock

    config = MagicMock()
    config.sleep_importance_reeval_enabled = True
    config.importance_ema_alpha = 0.3
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    return config


async def _reevaluate(db, owner, ws, ctx, scores=None) -> dict:
    phase = ImportanceReevalPhase(db, AsyncMock())
    phase._chunk_cascade = AsyncMock(return_value=False)

    async def judge(batch, *args, **kwargs):
        # The judge answers; it scores the rows ``scores`` names.
        phase._judged.extend(m.id for m in batch)
        return {m.id: scores[m.id] for m in batch if scores and m.id in scores}

    phase._judged = []
    phase._evaluate_batch = judge
    with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", new=AsyncMock()):
        result = await phase.execute(
            _reeval_config(), owner, str(ws.id), str(ctx.id), SleepBudget(max_llm_calls=1)
        )
    await db.flush()
    return {"result": result, "judged": list(phase._judged)}


@pytest.mark.asyncio
async def test_reevaluation_cadence_with_one_call_a_night(db_session):
    """25 due rows, one call a night: 10, 10, 5, then none inside the window;
    a row edited inside the window is not due."""
    owner, ws, ctx = await _scope(db_session)
    old = utcnow() - timedelta(days=STALENESS_DAYS + 30)
    rows = [_memory(owner, ws, ctx, created_at=old + timedelta(minutes=i)) for i in range(25)]
    edited = _memory(owner, ws, ctx, created_at=old, updated_at=utcnow() - timedelta(days=1))
    db_session.add_all([*rows, edited])
    await db_session.flush()

    nights = [await _reevaluate(db_session, owner, ws, ctx) for _ in range(4)]

    assert [len(n["judged"]) for n in nights] == [10, 10, 5, 0]
    assert [n["result"].details.get("more_due") for n in nights[:3]] == [True, True, False]
    judged = [mid for n in nights for mid in n["judged"]]
    assert len(set(judged)) == 25
    assert edited.id not in judged
    # Oldest first: the first night took the ten oldest rows.
    assert nights[0]["judged"] == [m.id for m in rows[:10]]


@pytest.mark.asyncio
async def test_reevaluation_is_not_an_update(db_session):
    """A re-evaluated row keeps ``updated_at`` NULL and changes_since lists no
    ``updated`` event for it."""
    owner, ws, ctx = await _scope(db_session)
    old = utcnow() - timedelta(days=STALENESS_DAYS + 30)
    rescored = _memory(owner, ws, ctx, created_at=old)
    db_session.add(rescored)
    await db_session.flush()
    since = utcnow() - timedelta(minutes=1)

    night = await _reevaluate(db_session, owner, ws, ctx, scores={rescored.id: 0.9})

    assert night["result"].details["updated"] == 1
    row = (
        await db_session.execute(
            select(Memory.importance, Memory.updated_at, Memory.importance_reevaluated_at).where(
                Memory.id == rescored.id
            )
        )
    ).one()
    assert row.importance == pytest.approx(0.3 * 0.9 + 0.7 * 0.5)
    assert row.updated_at is None
    assert row.importance_reevaluated_at is not None
    page = await changes_since(
        db_session, context_id=ctx.id, owner_user_id=None, since=since, until=None
    )
    assert [c for c in page.changes if c.memory_id == rescored.id and c.kind == "updated"] == []
