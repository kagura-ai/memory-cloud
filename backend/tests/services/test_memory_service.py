"""Tests for MemoryService."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import (
    ForgetRequest,
    RecallRequest,
    RememberRequest,
)
from services.memory_service import MemoryService


class TestMemoryServiceInit:
    """Test MemoryService initialization."""

    def test_init(self):
        """Test MemoryService creates all sub-services."""
        mock_db = MagicMock()
        service = MemoryService(mock_db)
        assert service.db == mock_db
        assert service.memory_repo is not None
        assert service.embedding_service is not None
        assert service.search_service is not None


class TestRecall:
    """Test recall (search) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_recall_requires_context(self, service):
        """recall() requires current_workspace_id and current_context_id."""
        request = RecallRequest(query="test query", k=5)

        with pytest.raises(ValueError, match="requires current_workspace_id"):
            await service.recall(request=request, user_id="test_user")

    @pytest.mark.asyncio
    async def test_recall_basic(self, service, context_id, workspace_id):
        """Test basic recall with mocked search."""
        request = RecallRequest(query="test query", k=5)

        memory_id = str(uuid4())

        # Mock search results
        search_results = [
            {
                "id": memory_id,
                "score": 0.9,
                "hybrid_score": 0.9,
                "payload": {
                    "summary": "Test result",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            }
        ]
        service.search_service.hybrid_search = AsyncMock(return_value=search_results)

        # Mock DB execute for PostgreSQL memory fetch
        mock_memory = MagicMock()
        mock_memory.id = memory_id
        mock_memory.summary = "Test result"
        mock_memory.context_summary = None
        mock_memory.type = "code"
        mock_memory.importance = 0.8
        mock_memory.scope = "working"
        mock_memory.created_at = datetime.utcnow()
        mock_memory.client = "test"
        mock_memory.tags = []
        mock_memory.context = None
        mock_memory.source_uri = None
        mock_memory.source_type = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_memory]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        service.db.execute = AsyncMock(return_value=mock_result)
        service.db.commit = AsyncMock()

        service.memory_repo.update_access_stats = AsyncMock()
        service._check_and_promote = AsyncMock()

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert response.results is not None
        assert len(response.results) > 0

    @pytest.mark.asyncio
    async def test_recall_no_results(self, service, context_id, workspace_id):
        """Test recall with no results."""
        request = RecallRequest(query="nonexistent", k=5)

        mock_context = MagicMock(
            id=context_id, workspace_id=workspace_id, is_private=True, created_by="test_user"
        )
        service.context_service.get_context = AsyncMock(return_value=mock_context)
        service._get_context_search_config = AsyncMock(return_value=None)
        service.search_service.hybrid_search = AsyncMock(return_value=[])

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert len(response.results) == 0


class TestRemember:
    """Test remember (store) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_remember_requires_context(self, service):
        """remember() requires current_context_id."""
        request = RememberRequest(
            summary="Test memory for search",
            content="Test content body",
            type="code",
        )

        with pytest.raises(ValueError, match="requires current_context_id"):
            await service.remember(
                request=request,
                user_id="test_user",
            )


class TestReference:
    """Test reference (get details) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_reference_not_found(self, service):
        """reference() with nonexistent memory raises NotFoundException."""
        from utils.exceptions import NotFoundException

        memory_id = uuid4()
        service.memory_repo.get = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException):
            await service.reference(memory_id=memory_id, user_id="test_user")

    @pytest.mark.asyncio
    async def test_reference_found(self, service):
        """reference() returns full memory details."""
        memory_id = uuid4()
        mock_memory = MagicMock(
            id=memory_id,
            user_id="test_user",
            summary="Test",
            content="Test content",
            context_summary="Context",
            details={"key": "value"},
            type="code",
            importance=0.8,
            tags=["python"],
            context={"description": "test context"},
            scope="working",
            client="claude",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            embedding_status="success",
            workspace_id=uuid4(),
            context_id=uuid4(),
            deleted_at=None,
        )
        service.memory_repo.get = AsyncMock(return_value=mock_memory)
        service.memory_repo.update_access_stats = AsyncMock()
        service.db.commit = AsyncMock()

        with patch("services.permission_service.PermissionService") as mock_perm_cls:
            mock_perm = MagicMock()
            mock_perm.can_access_memory = AsyncMock(return_value=True)
            mock_perm_cls.return_value = mock_perm

            response = await service.reference(memory_id=memory_id, user_id="test_user")

        assert response.memory_id == memory_id
        assert response.summary == "Test"


class TestRememberRequest:
    """Test RememberRequest schema validation for #213/#215 fields."""

    def test_source_uri_accepted(self):
        """source_uri and source_type are accepted as optional fields."""
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            source_uri="vault://my-vault/note.md",
            source_type="vault",
        )
        assert req.source_uri == "vault://my-vault/note.md"
        assert req.source_type == "vault"

    def test_source_fields_optional(self):
        """source_uri/source_type default to None."""
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
        )
        assert req.source_uri is None
        assert req.source_type is None

    def test_invalid_source_type_rejected(self):
        """Invalid source_type is rejected by Literal validation."""
        with pytest.raises(ValueError):
            RememberRequest(
                summary="Test memory for search quality",
                content="Test content",
                type="note",
                source_type="invalid_type",
            )

    def test_linked_fields_accepted(self):
        """linked_memory_ids and linked_source_uris are accepted."""
        target_id = uuid4()
        req = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[target_id],
            linked_source_uris=["vault://my-vault/other.md"],
        )
        assert req.linked_memory_ids == [target_id]
        assert req.linked_source_uris == ["vault://my-vault/other.md"]


class TestDeclaredLinks:
    """Test _create_declared_links logic (#215)."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_skips_when_no_links(self, service):
        """No-op when neither linked_memory_ids nor linked_source_uris provided."""
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
        )
        # Should return immediately without touching DB
        await service._create_declared_links(
            memory_id=uuid4(),
            request=request,
            user_id="test_user",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        service.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_without_isolation(self, service):
        """Skips when workspace_id or context_id is None."""
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[uuid4()],
        )
        await service._create_declared_links(
            memory_id=uuid4(),
            request=request,
            user_id="test_user",
            workspace_id=None,
            context_id=None,
        )
        service.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_link_filtered(self, service):
        """Self-links are filtered out before DB query."""
        memory_id = uuid4()
        request = RememberRequest(
            summary="Test memory for search quality",
            content="Test content",
            type="note",
            linked_memory_ids=[memory_id],  # self-link
        )
        # Mock the validation query to return empty (self-link filtered before query)
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        service.db.execute = AsyncMock(return_value=mock_result)

        await service._create_declared_links(
            memory_id=memory_id,
            request=request,
            user_id="test_user",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        # The empty requested_ids list means no DB query at all
        service.db.execute.assert_not_called()


class TestForget:
    """Test forget (delete) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_forget_by_id_not_found(self, service):
        """forget() with nonexistent memory returns empty response (no exception)."""
        memory_id = uuid4()
        request = ForgetRequest(memory_id=memory_id)
        # _get_context_isolation_params calls context_service.get_context only when
        # current_context_id is provided; here it is None so returns (None, None, None)
        service.memory_repo.get = AsyncMock(return_value=None)
        service.db.commit = AsyncMock()

        response = await service.forget(request=request, user_id="test_user")
        assert response.deleted_count == 0
        assert response.memory_ids == []
