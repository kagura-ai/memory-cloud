"""The write responses carry the stored ``observed_at`` (#2689).

``remember`` / ``remember_batch`` / ``update_memory`` echo it on MCP with its
weekday, so the service must put the STORED value — after the fold and the
UTC normalisation — on ``RememberResponse`` / ``UpdateMemoryResponse``. These
drive the real service methods (a mocked service handing the field to the
handler would pass even if the service never set it). REST ``/remember``
serialises ``RememberResponse``: the field is excluded there, so the REST
response and its OpenAPI schema stay as they were.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import RememberRequest, RememberResponse, UpdateMemoryRequest
from services.memory_service import MemoryService
from services.persistence import persistence_info


@pytest.fixture
def service():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()

    svc = MemoryService(db)
    context = MagicMock()
    context.id = uuid4()
    context.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(context, str(context.workspace_id), str(context.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc._create_declared_links = AsyncMock()
    svc._mock_context = context
    return svc


async def _remember(service, **kwargs):
    request = RememberRequest(
        summary="went hiking with the team", content="last weekend", type="event", **kwargs
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch(
            "services.quota_service.QuotaService",
            **{
                "return_value.check_memory_quota": AsyncMock(return_value=(True, None)),
                "return_value.check_memories_per_day": AsyncMock(return_value=(True, None)),
            },
        ),
    ):
        return await service.remember(
            request,
            user_id="test_user",
            client="test",
            current_context_id=service._mock_context.id,
            current_workspace_id=None,
        )


class TestRemember:
    @pytest.mark.asyncio
    async def test_the_argument_comes_back_in_its_stored_form(self, service):
        result = await _remember(service, observed_at="2024-03-11T23:30:00+09:00")
        assert result.observed_at == "2024-03-11T14:30:00Z"

    @pytest.mark.asyncio
    async def test_a_details_date_comes_back_as_written(self, service):
        result = await _remember(service, details={"observed_at": "2024-03-11"})
        assert result.observed_at == "2024-03-11"

    @pytest.mark.asyncio
    async def test_none_without_one(self, service):
        result = await _remember(service)
        assert result.observed_at is None

    def test_rest_serialisation_leaves_it_out(self):
        response = RememberResponse(memory_id=uuid4(), scope="working", observed_at="2024-03-11")
        assert "observed_at" not in response.model_dump()
        assert "observed_at" not in response.model_dump(mode="json")
        schema = RememberResponse.model_json_schema(mode="serialization")
        assert "observed_at" not in schema["properties"]


def _stored_memory(details):
    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "Original summary for testing"
    memory.context_summary = None
    memory.content = "Original content"
    memory.details = details
    memory.type = "event"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    memory.created_at = None
    memory.updated_at = None
    memory.deleted_at = None
    memory.embedding_status = "success"
    memory.version = 1
    return memory


async def _update_in_place(service, memory, **fields):
    service.memory_repo.get = AsyncMock(return_value=memory)
    with (
        patch("services.permission_service.PermissionService") as perm_cls,
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
    ):
        perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)
        return await service._update_in_place(
            UpdateMemoryRequest(memory_id=memory.id, **fields), user_id="test_user"
        )


class TestUpdateInPlace:
    @pytest.mark.asyncio
    async def test_the_stored_value_comes_back_on_an_unrelated_edit(self, service):
        memory = _stored_memory({"observed_at": "2024-03-11"})
        result = await _update_in_place(service, memory, importance=0.9)
        assert result.observed_at == "2024-03-11"

    @pytest.mark.asyncio
    async def test_a_new_value_comes_back_in_its_stored_form(self, service):
        memory = _stored_memory(None)
        result = await _update_in_place(service, memory, observed_at="2024-03-14T08:00:00+09:00")
        assert result.observed_at == "2024-03-13T23:00:00Z"

    @pytest.mark.asyncio
    async def test_none_without_one(self, service):
        memory = _stored_memory(None)
        result = await _update_in_place(service, memory, importance=0.9)
        assert result.observed_at is None


class TestUpsert:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("observed_at", ["2024-03-11", None])
    async def test_the_new_rows_value_is_carried(self, service, observed_at):
        service.memory_repo.list_by_resource_id = AsyncMock(return_value=[])
        remembered = RememberResponse(
            memory_id=uuid4(),
            scope="working",
            persistence=persistence_info("working"),
            observed_at=observed_at,
        )
        service.remember = AsyncMock(return_value=remembered)

        result = await service._upsert_by_external_id(
            UpdateMemoryRequest(
                external_id="doc-1",
                summary="Brand new memory for the upsert path",
                content="content",
                type="note",
            ),
            user_id="test_user",
        )

        assert result.observed_at == observed_at


def test_every_write_response_construction_carries_observed_at():
    """A construction site added later without it would drop the echo silently."""
    import services.memory_service as memory_service

    tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"RememberResponse", "UpdateMemoryResponse"}
    ]
    assert len(sites) >= 4, f"expected the known write-response sites, found {len(sites)}"
    for call in sites:
        assert any(kw.arg == "observed_at" for kw in call.keywords), (
            f"{call.func.id} at line {call.lineno} is built without observed_at"
        )
