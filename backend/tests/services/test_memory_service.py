"""Tests for MemoryService."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
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

        # Mock context lookup
        mock_context = MagicMock(
            id=context_id, workspace_id=workspace_id, is_private=True, created_by="test_user"
        )
        service.context_service.get_context = AsyncMock(return_value=mock_context)
        service._get_context_search_config = AsyncMock(return_value=None)

        # Mock search results
        search_results = [
            {
                "id": str(uuid4()),
                "score": 0.9,
                "payload": {
                    "summary": "Test result",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            }
        ]
        service.search_service.hybrid_search = AsyncMock(return_value=search_results)

        response = await service.recall(
            request=request,
            user_id="test_user",
            current_context_id=context_id,
            current_workspace_id=workspace_id,
        )

        assert response.results is not None
        assert response.total > 0

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

        assert response.total == 0
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
        service.memory_repo.get_by_id = AsyncMock(return_value=None)

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
            context="test context",
            scope="working",
            client="claude",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            embedding_status="success",
        )
        service.memory_repo.get_by_id = AsyncMock(return_value=mock_memory)

        response = await service.reference(memory_id=memory_id, user_id="test_user")
        assert response.memory_id == memory_id
        assert response.summary == "Test"


class TestForget:
    """Test forget (delete) operations."""

    @pytest.fixture
    def service(self):
        return MemoryService(MagicMock())

    @pytest.mark.asyncio
    async def test_forget_by_id_not_found(self, service):
        """forget() with nonexistent memory raises NotFoundException."""
        from utils.exceptions import NotFoundException

        memory_id = uuid4()
        request = ForgetRequest(memory_id=memory_id)
        service.memory_repo.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException):
            await service.forget(request=request, user_id="test_user")
