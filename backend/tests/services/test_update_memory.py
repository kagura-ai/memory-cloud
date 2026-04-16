"""Tests for update_memory (Issue #80)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest
from services.memory_service import MemoryService


class TestUpdateMemoryInPlace:
    """Test in-place update by memory_id."""

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
        """Create a mock Memory object with sensible defaults."""
        memory = MagicMock()
        memory.id = overrides.get("id", uuid4())
        memory.user_id = overrides.get("user_id", "test_user")
        memory.workspace_id = overrides.get("workspace_id", uuid4())
        memory.context_id = overrides.get("context_id", uuid4())
        memory.summary = overrides.get("summary", "Original summary for testing")
        memory.context_summary = overrides.get("context_summary", None)
        memory.content = overrides.get("content", "Original content")
        memory.details = overrides.get("details", None)
        memory.type = overrides.get("type", "note")
        memory.importance = overrides.get("importance", 0.5)
        memory.tags = overrides.get("tags", ["original"])
        memory.context = overrides.get("context", None)
        memory.scope = overrides.get("scope", "working")
        memory.client = overrides.get("client", "mcp")
        memory.created_at = None
        memory.updated_at = None
        memory.deleted_at = None
        memory.embedding_status = "success"
        return memory

    @pytest.mark.asyncio
    async def test_metadata_only_update_no_reembed(self, service):
        """Tags/importance change should not trigger re-embedding."""
        memory = self._make_memory()
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

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                tags=["updated", "tags"],
                importance=0.9,
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.operation == "updated"
            assert result.re_embedded is False
            assert memory.embedding_status == "success"  # unchanged
            mock_payload_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_summary_change_triggers_reembed(self, service, mock_db):
        """Summary change should set embedding_status=pending and fire create_task."""
        memory = self._make_memory(summary="Original summary for testing")
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                summary="Completely different summary that triggers re-embedding",
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.operation == "updated"
            assert result.re_embedded is True
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_context_summary_change_triggers_reembed(self, service, mock_db):
        """context_summary change should trigger re-embedding (BM25 tokens)."""
        memory = self._make_memory(context_summary="Old context summary")
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                context_summary="Updated context summary with new info",
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.re_embedded is True


class TestUpsertByExternalId:
    """Test upsert by external_id."""

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

    @pytest.mark.asyncio
    async def test_upsert_creates_when_not_exists(self, service):
        """When external_id not found, should create new memory."""
        service.memory_repo.get_by_resource_id = AsyncMock(return_value=None)

        new_memory_id = uuid4()
        mock_remember_response = MagicMock()
        mock_remember_response.memory_id = new_memory_id
        mock_remember_response.scope = "working"
        service.remember = AsyncMock(return_value=mock_remember_response)

        ctx_id = uuid4()
        ws_id = uuid4()
        request = UpdateMemoryRequest(
            external_id="new-resource",
            summary="Brand new memory for upsert test",
            content="Content for new memory",
            type="note",
        )

        result = await service._upsert_by_external_id(
            request,
            user_id="test_user",
            client="mcp",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )

        assert result.operation == "created"
        assert result.memory_id == new_memory_id
        service.remember.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_replaces_when_exists(self, service):
        """When external_id found, should remember new then forget old."""
        existing = MagicMock()
        existing.id = uuid4()
        service.memory_repo.get_by_resource_id = AsyncMock(return_value=existing)

        new_memory_id = uuid4()
        mock_remember_response = MagicMock()
        mock_remember_response.memory_id = new_memory_id
        mock_remember_response.scope = "working"
        service.remember = AsyncMock(return_value=mock_remember_response)

        mock_forget_response = MagicMock()
        mock_forget_response.deleted_count = 1
        service.forget = AsyncMock(return_value=mock_forget_response)

        ctx_id = uuid4()
        ws_id = uuid4()
        request = UpdateMemoryRequest(
            external_id="existing-resource",
            summary="Replacement memory for upsert test",
            content="New replacement content",
            type="note",
        )

        result = await service._upsert_by_external_id(
            request,
            user_id="test_user",
            client="mcp",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )

        assert result.operation == "replaced"
        assert result.memory_id == new_memory_id
        # remember should be called before forget (data safety)
        service.remember.assert_called_once()
        service.forget.assert_called_once()


class TestUpdateMemoryRequestValidation:
    """Test schema validation for UpdateMemoryRequest."""

    def test_requires_identifier(self):
        """Must provide either memory_id or external_id."""
        with pytest.raises(ValueError, match="Either memory_id or external_id"):
            UpdateMemoryRequest()

    def test_rejects_both_identifiers(self):
        """Cannot provide both memory_id and external_id."""
        with pytest.raises(ValueError, match="either memory_id or external_id"):
            UpdateMemoryRequest(
                memory_id=uuid4(),
                external_id="some-id",
                summary="Test summary for validation",
                content="Test content",
                type="note",
            )

    def test_upsert_requires_summary_content_type(self):
        """external_id mode requires summary, content, type."""
        with pytest.raises(ValueError, match="summary, content, and type are required"):
            UpdateMemoryRequest(
                external_id="some-id",
                summary="Has summary for test",
                # missing content and type
            )

    def test_upsert_rejects_empty_strings(self):
        """external_id mode rejects empty string values."""
        with pytest.raises(ValueError, match="summary, content, and type are required"):
            UpdateMemoryRequest(
                external_id="some-id",
                summary="Has summary for test",
                content="   ",  # whitespace only
                type="note",
            )

    def test_memory_id_mode_accepts_partial(self):
        """memory_id mode allows partial updates."""
        req = UpdateMemoryRequest(
            memory_id=uuid4(),
            tags=["new-tag"],
        )
        assert req.tags == ["new-tag"]
        assert req.summary is None
