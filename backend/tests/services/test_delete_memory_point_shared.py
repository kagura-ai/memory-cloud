"""#1869: the shared-point check behind forget, against real rows.

A resource-ingested memory's point id is ``uuid5(resource:doc:version)``
(#1829), so one document indexed into two contexts that share a collection is
ONE point for TWO rows. ``tests/services/test_memory_service.py`` pins the
branching with a mocked session; this pins the query itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from models.auth import Context, Workspace
from models.memory import Memory
from services.memory_service import MemoryService
from utils.datetime import utcnow


async def _context(db_session, workspace: Workspace) -> Context:
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"shared_{uuid4().hex[:8]}",
        display_name="Shared point",
        created_by="shared_point_user",
    )
    db_session.add(context)
    await db_session.flush()
    return context


def _resource_memory(context: Context, point_id, **overrides) -> Memory:
    fields = {
        "id": uuid4(),
        "user_id": "shared_point_user",
        "workspace_id": context.workspace_id,
        "context_id": context.id,
        "summary": "[res_1] doc_1 v1",
        "summary_embedding_id": point_id,
        "content": "c",
        "type": "note",
        "client": "pytest",
        "embedding_status": "success",
        "details": {"resource_id": "res_1", "doc_id": "doc_1", "version": "1"},
    }
    fields.update(overrides)
    return Memory(**fields)


async def _delete_point(db_session, memory: Memory) -> AsyncMock:
    with (
        patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
        patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()) as delete_point,
    ):
        await MemoryService(db_session)._delete_memory_point("shared_point_user", memory)
    return delete_point


class TestDeleteMemoryPointSharedCheck:
    @pytest.mark.asyncio
    async def test_a_point_a_live_row_in_another_context_names_is_kept(self, db_session):
        workspace = Workspace(id=uuid4(), name="Shared", owner_user_id="shared_point_user")
        db_session.add(workspace)
        await db_session.flush()
        first, second = await _context(db_session, workspace), await _context(db_session, workspace)
        point_id = uuid4()
        forgotten = _resource_memory(first, point_id)
        db_session.add_all([forgotten, _resource_memory(second, point_id)])
        await db_session.flush()

        delete_point = await _delete_point(db_session, forgotten)

        delete_point.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_point_only_tombstones_and_other_points_name_is_deleted(self, db_session):
        workspace = Workspace(id=uuid4(), name="Shared", owner_user_id="shared_point_user")
        db_session.add(workspace)
        await db_session.flush()
        first, second = await _context(db_session, workspace), await _context(db_session, workspace)
        point_id = uuid4()
        forgotten = _resource_memory(first, point_id)
        db_session.add_all(
            [
                forgotten,
                # A tombstone under the same point, and a live row under another.
                _resource_memory(second, point_id, deleted_at=utcnow()),
                _resource_memory(second, uuid4()),
            ]
        )
        await db_session.flush()

        delete_point = await _delete_point(db_session, forgotten)

        delete_point.assert_awaited_once_with("shared_point_user", point_id, collection_name="c")
