"""Qdrant ``location`` payload sync on details writes (#1332).

The metadata-only update branch patches Qdrant payload for tags/importance/
type/context_summary but historically ignored ``details`` — a location edit
through update_memory/patch_memory would leave the geo payload stale and the
memory wrongly included/excluded by ``filters.near``. These pins cover the
pure helper and the ``_update_in_place`` wiring (both set and remove).
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest
from services.memory_service import MemoryService, location_payload_from_details


class TestLocationPayloadFromDetails:
    def test_full_pair_returns_payload_value(self):
        assert location_payload_from_details(
            {"location": {"lat": 35.6, "lon": 139.7, "label": "Tokyo"}}
        ) == {"lat": 35.6, "lon": 139.7}

    def test_no_details_or_no_location_returns_none(self):
        assert location_payload_from_details(None) is None
        assert location_payload_from_details({}) is None
        assert location_payload_from_details({"other": 1}) is None

    def test_malformed_location_returns_none(self):
        # Raw legacy rows predating the contract: never raise here — the
        # payload simply carries no geo field (point excluded from near).
        assert location_payload_from_details({"location": "Tokyo office"}) is None
        assert location_payload_from_details({"location": {"lat": 35.6}}) is None
        assert location_payload_from_details({"location": {"lat": "35.6", "lon": "139"}}) is None


class TestUpdateInPlaceLocationSync:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return MemoryService(mock_db)

    def _make_memory(self, **overrides):
        memory = MagicMock()
        memory.id = overrides.get("id", uuid4())
        memory.user_id = "test_user"
        memory.workspace_id = uuid4()
        memory.context_id = uuid4()
        memory.summary = "Original summary for testing"
        memory.context_summary = None
        memory.content = "Original content"
        memory.details = overrides.get("details", None)
        memory.type = "note"
        memory.importance = 0.5
        memory.tags = ["original"]
        memory.context = None
        memory.scope = "working"
        memory.client = "mcp"
        memory.created_at = None
        memory.updated_at = None
        memory.deleted_at = None
        memory.embedding_status = "success"
        return memory

    async def _run(self, service, memory, request):
        service.memory_repo.get = AsyncMock(return_value=memory)
        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()
            ) as mock_payload_update,
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            result = await service._update_in_place(request, user_id="test_user")
        return result, mock_payload_update

    @pytest.mark.asyncio
    async def test_details_location_write_syncs_geo_payload(self, service):
        memory = self._make_memory()
        request = UpdateMemoryRequest(
            memory_id=memory.id,
            details={"location": {"lat": 35.6812, "lon": 139.7671}},
        )
        result, mock_payload_update = await self._run(service, memory, request)
        assert result.re_embedded is False
        kwargs = mock_payload_update.await_args.kwargs
        assert kwargs["payload_updates"]["location"] == {
            "lat": pytest.approx(35.6812),
            "lon": pytest.approx(139.7671),
        }

    @pytest.mark.asyncio
    async def test_details_write_removing_location_deletes_geo_payload(self, service):
        memory = self._make_memory(details={"location": {"lat": 35.6, "lon": 139.7}})
        request = UpdateMemoryRequest(memory_id=memory.id, details={"note": "moved away"})
        _, mock_payload_update = await self._run(service, memory, request)
        kwargs = mock_payload_update.await_args.kwargs
        assert "location" not in kwargs.get("payload_updates", {})
        assert kwargs["delete_keys"] == ["location"]

    @pytest.mark.asyncio
    async def test_no_details_in_request_leaves_geo_payload_untouched(self, service):
        memory = self._make_memory(details={"location": {"lat": 35.6, "lon": 139.7}})
        request = UpdateMemoryRequest(memory_id=memory.id, importance=0.9)
        _, mock_payload_update = await self._run(service, memory, request)
        kwargs = mock_payload_update.await_args.kwargs
        assert "location" not in kwargs.get("payload_updates", {})
        assert not kwargs.get("delete_keys")
