"""Regression: merge_contexts must not copy memories via dropped columns.

v0.34.0 review finding (#1046 fallout): ``ContextService.merge_contexts``
constructed each copied memory as ``Memory(..., use_count=0, ...)``, but #1046
dropped ``use_count`` from the ``Memory`` ORM model. The copy loop therefore
raised ``TypeError: 'use_count' is an invalid keyword argument for Memory`` for
any merge of a context that actually holds memories.

It shipped because every existing ``merge_contexts`` test mocks the service or
exercises only the empty-source early-return — the memory-copy loop had zero
coverage. This test drives the REAL copy loop with a REAL ``Memory`` model (DB,
Qdrant and permission checks mocked), so a future column drift in the copy
field-set fails here instead of in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.memory import Memory
from services.context_service import ContextService
from utils.datetime import utcnow


def _source_memory(user_id: str, source_context_id) -> Memory:
    """A real (transient) Memory the copy loop will read its fields from."""
    return Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=uuid4(),
        context_id=source_context_id,
        summary="copied summary",
        context_summary="why it matters",
        content="full content",
        context={"context_id": str(source_context_id)},
        details=None,
        type="note",
        importance=0.6,
        confidence=0.9,
        tags=["a", "b"],
        scope="persistent",
        long_term=True,
        promoted_at=None,
        client="test",
        client_version="1",
        source="manual",
        created_at=utcnow(),
        embedding_status="success",
    )


@pytest.mark.asyncio
async def test_merge_contexts_copies_memory_without_dropped_use_count():
    user_id = "user-merge"
    source_id = uuid4()
    target_id = uuid4()
    workspace_id = uuid4()

    source_ctx = MagicMock(id=source_id, workspace_id=workspace_id, is_locked=False)
    target_ctx = MagicMock(id=target_id, workspace_id=workspace_id, is_locked=False)

    mock_db = AsyncMock()
    # First execute() = embedding-config query (none → both use settings defaults,
    # so the same-model check passes); second = source-memory query.
    cfg_result = MagicMock()
    cfg_result.scalars.return_value.all.return_value = []
    mem_result = MagicMock()
    mem_result.scalars.return_value.all.return_value = [_source_memory(user_id, source_id)]
    mock_db.execute.side_effect = [cfg_result, mem_result]
    mock_db.add = MagicMock()  # synchronous in SQLAlchemy

    service = ContextService(mock_db)

    with (
        patch.object(
            service, "get_context", new_callable=AsyncMock, side_effect=[source_ctx, target_ctx]
        ),
        patch("services.permission_service.PermissionService") as PermSvc,
        patch("db.qdrant.copy_context_points", new_callable=AsyncMock, return_value=1),
        patch("db.qdrant.get_collection_name", return_value="collection"),
    ):
        PermSvc.return_value.check_context_owner = AsyncMock()
        result = await service.merge_contexts(user_id, source_id, target_id)

    assert result["merged"] == 1
    # The copy loop built and added exactly one new Memory (no TypeError on the
    # dropped column), retargeted to the destination context with stats reset.
    mock_db.add.assert_called_once()
    new_mem = mock_db.add.call_args.args[0]
    assert isinstance(new_mem, Memory)
    assert new_mem.context_id == target_id
    assert new_mem.access_count == 0
    assert not hasattr(new_mem, "use_count")
