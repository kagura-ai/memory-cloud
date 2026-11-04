"""Preconditions for a practice write from a browser that may have switched workspaces."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.contexts import ContextCreate, create_context
from models.schemas import RememberRequest
from services.memory_service import MemoryService
from utils.exceptions import ConflictError


@pytest.mark.asyncio
async def test_stale_create_rejected_before_quota_or_insertion():
    service = MagicMock()
    with pytest.raises(HTTPException) as error:
        await create_context(
            ContextCreate(name="practice", expected_workspace_id=uuid4()),
            {"user_id": "owner", "current_workspace_id": uuid4()},
            service=service,
        )
    assert error.value.status_code == 409
    assert not service.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("target_changed_during_preparation", [False, True])
async def test_private_target_rechecked_before_insert(target_changed_during_preparation):
    db = AsyncMock()
    workspace, context = uuid4(), uuid4()
    db.scalar.return_value = None
    service = MemoryService.__new__(MemoryService)
    service.db = db
    service.memory_repo = MagicMock()
    with (
        patch.object(service, "_get_context_isolation_params", new_callable=AsyncMock) as access,
        patch.object(service.memory_repo, "create", new_callable=AsyncMock) as insert,
        pytest.raises(ConflictError),
    ):
        access.return_value = (MagicMock(), str(workspace), str(context))
        if target_changed_during_preparation:
            # Isolate the final lock check after the wider preparation has completed.
            service._prepare_remember = AsyncMock()  # type: ignore[method-assign]
        await service.remember(
            RememberRequest(summary="An example memory", content="Example", type="note"),
            user_id="owner",
            current_context_id=context,
            current_workspace_id=workspace,
            private_workspace_id=workspace,
        )
    assert access.await_count == int(not target_changed_during_preparation)
    insert.assert_not_awaited()
    query = str(db.scalar.await_args.args[0])
    for predicate in ("workspace_id", "created_by", "is_private", "is_public", "role"):
        assert predicate in query
    if target_changed_during_preparation:
        assert "FOR UPDATE" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [False, True])
async def test_private_condition_never_runs_for_contexts_denied_by_normal_access(exists):
    from utils.exceptions import NotFoundException

    service = MemoryService.__new__(MemoryService)
    service.db = AsyncMock()
    context = uuid4()
    workspace = uuid4()
    service.db.scalar.return_value = context if exists else None
    with (
        patch.object(
            service, "_get_context_isolation_params", side_effect=NotFoundException("Context")
        ),
        pytest.raises(NotFoundException),
    ):
        await service.remember(
            RememberRequest(summary="An example memory", content="Example", type="note"),
            user_id="restricted-agent",
            current_context_id=context,
            current_workspace_id=workspace,
            private_workspace_id=workspace,
        )
    service.db.scalar.assert_not_awaited()
