"""Write paths × details.observed_at against the real DB (#2330).

- remember with the top-level argument fills the details and the column;
- update_memory(observed_at=...) alone is a details edit (D7): a new version
  whose revision prior holds the old details, the other keys kept, and
  reference(version=N) shows that version's own value;
- PATCH with observed_at alone does the same;
- build_memory_point reads the generated column.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import SOURCE_TYPE_MANUAL, Memory, MemoryRevision
from models.schemas import PatchMemoryRequest, UpdateMemoryRequest
from services.memory_service import MemoryService, build_memory_point


@pytest_asyncio.fixture(loop_scope="session")
async def env(db_session):
    uid = f"e2e-observed-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()
    db_session.add(
        User(
            email=f"{uid}@example.test",
            user_id=uid,
            name="Observed At E2E",
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
    ctx = Context(id=uuid.uuid4(), workspace_id=ws_id, name="observed", created_by=uid)
    db_session.add(ctx)
    await db_session.flush()
    memory = Memory(
        id=uuid.uuid4(),
        user_id=uid,
        workspace_id=ws_id,
        context_id=ctx.id,
        summary="the price was 100",
        content="the price was 100",
        type="fact",
        client="test",
        tags=[],
        source_type=SOURCE_TYPE_MANUAL,
        details={"note": "keep", "observed_at": "2020-01-01"},
        embedding_status="success",
    )
    db_session.add(memory)
    await db_session.flush()
    return {"uid": uid, "ctx": ctx.id, "memory": memory.id}


def _side_effects():
    return (
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
    )


async def _column(db_session, mem_id) -> str | None:
    return (
        await db_session.execute(
            text("SELECT observed_at FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).scalar_one()


@pytest.mark.asyncio(loop_scope="session")
async def test_observed_at_alone_is_a_versioned_details_edit(env, db_session):
    svc = MemoryService(db_session)
    p1, p2 = _side_effects()
    with p1 as payload, p2:
        response = await svc.update_memory(
            UpdateMemoryRequest(memory_id=env["memory"], observed_at="2024-03-07T10:00:00+09:00"),
            user_id=env["uid"],
            current_context_id=env["ctx"],
        )
    assert response.version == 2
    assert await _column(db_session, env["memory"]) == "2024-03-07T01:00:00Z"
    stored = await db_session.get(Memory, env["memory"])
    await db_session.refresh(stored)
    assert stored.details == {"note": "keep", "observed_at": "2024-03-07T01:00:00Z"}
    assert stored.observed_at == "2024-03-07T01:00:00Z"
    assert payload.await_args.kwargs["payload_updates"]["observed_at"] == "2024-03-07T01:00:00Z"

    prior = (
        await db_session.execute(
            select(MemoryRevision.prior).where(
                MemoryRevision.memory_id == env["memory"], MemoryRevision.version == 2
            )
        )
    ).scalar_one()
    assert prior["details"] == {"note": "keep", "observed_at": "2020-01-01"}

    with patch.object(svc, "_audit_reference_read", AsyncMock()):
        current = await svc.reference(env["memory"], env["uid"], record_access=False)
        earlier = await svc.reference(env["memory"], env["uid"], record_access=False, version=1)
    assert current.observed_at == "2024-03-07T01:00:00Z"
    assert earlier.historical is True
    assert earlier.observed_at == "2020-01-01"

    point_payload, _, _ = build_memory_point(stored)
    assert point_payload["observed_at"] == "2024-03-07T01:00:00Z"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_bad_value_writes_nothing(env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="invalid details.observed_at"):
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=env["memory"], observed_at="last spring"),
            user_id=env["uid"],
            current_context_id=env["ctx"],
        )
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_with_observed_at_alone(env, db_session):
    svc = MemoryService(db_session)
    p1, p2 = _side_effects()
    with p1, p2:
        response = await svc.patch_memory(
            env["memory"], PatchMemoryRequest(observed_at="2025-05-05"), user_id=env["uid"]
        )
    assert response.observed_at == "2025-05-05"
    assert await _column(db_session, env["memory"]) == "2025-05-05T00:00:00Z"
    stored = await db_session.get(Memory, env["memory"])
    await db_session.refresh(stored)
    assert stored.details["note"] == "keep"
