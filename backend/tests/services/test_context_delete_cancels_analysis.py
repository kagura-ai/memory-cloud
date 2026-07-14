"""#1241/#1243: deleting a context cancels its in-flight analysis run.

Without this, the background pipeline kept charging the workspace's
BYOK key after the user deleted the context — the strongest stop signal
they can send — and then persisted a full result set that #1243's
liveness join makes permanently invisible.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from models.analysis import MemoryAnalysis
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from models.llm_pricing import LLMPricing
from services.context_service import ContextService
from utils.datetime import utcnow


async def _seed(db_session):
    ws = Workspace(id=uuid4(), name="Del Test", owner_user_id="del_user")
    db_session.add(ws)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id="del_user", role=WorkspaceRole.OWNER)
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name="del_ctx",
        display_name="Del Context",
        created_by="del_user",
        is_private=False,
    )
    db_session.add(ctx)
    pricing = LLMPricing(
        provider="openai",
        model=f"gpt-test-{uuid4().hex[:8]}",
        unit_type="input_tokens",
        price_per_unit="0.001",
        currency="USD",
        effective_from=datetime(2024, 1, 1),
    )
    db_session.add(pricing)
    await db_session.flush()
    return ws, ctx, pricing


class TestDeleteContextCancelsRunningAnalysis:
    @pytest.mark.asyncio
    async def test_running_analysis_cancelled_and_task_stopped(self, db_session):
        ws, ctx, pricing = await _seed(db_session)
        run = MemoryAnalysis(
            workspace_id=ws.id,
            context_id=ctx.id,
            triggered_by="del_user",
            model_id=pricing.id,
            model_snapshot={},
            embedding_model="em",
            params={},
            input_count=0,
            status="running",
            paid_by="byok",
        )
        db_session.add(run)
        await db_session.flush()

        with patch("tasks.analysis_tasks.cancel_run_task", return_value=True) as mock_cancel:
            await ContextService(db_session).delete_context("del_user", ctx.id, _commit=False)

        assert run.status == "cancelled"
        assert run.cancellation_reason == "context_deleted"
        assert run.finished_at is not None
        mock_cancel.assert_called_once_with(run.id)

    @pytest.mark.asyncio
    async def test_terminal_runs_left_untouched(self, db_session):
        ws, ctx, pricing = await _seed(db_session)
        done = MemoryAnalysis(
            workspace_id=ws.id,
            context_id=ctx.id,
            triggered_by="del_user",
            model_id=pricing.id,
            model_snapshot={},
            embedding_model="em",
            params={},
            input_count=0,
            status="succeeded",
            paid_by="byok",
            finished_at=utcnow(),
        )
        db_session.add(done)
        await db_session.flush()

        with patch("tasks.analysis_tasks.cancel_run_task") as mock_cancel:
            await ContextService(db_session).delete_context("del_user", ctx.id, _commit=False)

        assert done.status == "succeeded"
        assert done.cancellation_reason is None
        mock_cancel.assert_not_called()
