"""Tests for MemoryService."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import (
    ForgetRequest,
    RecallRequest,
    ReferenceRequest,
    RememberRequest,
)
from services.memory_service import MemoryService


class TestMemoryService:
    """Test MemoryService for core memory operations."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        """Create MemoryService."""
        return MemoryService(mock_db)

    def test_init(self, mock_db):
        """Test MemoryService initialization."""
        service = MemoryService(mock_db)

        assert service.db == mock_db
        assert service.memory_repo is not None
        assert service.embedding_service is not None
        assert service.search_service is not None

    @pytest.mark.asyncio
    async def test_remember_basic(self, service):
        """Test basic memory creation."""
        request = RememberRequest(
            summary="Test memory",
            content="Test content",
            type="code",
            context_summary="Test context",
        )

        # Mock dependencies
        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

            with patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()):
                service.memory_repo.create = AsyncMock(
                    return_value=MagicMock(id=uuid4(), scope="working")
                )

                response = await service.remember(
                    request=request,
                    user_id="test_user",
                )

                # Check response
                assert response.memory_id is not None
                assert response.scope in ["working", "persistent"]
                assert response.message is not None

    @pytest.mark.asyncio
    async def test_remember_with_tags(self, service):
        """Test memory creation with tags."""
        request = RememberRequest(
            summary="Test memory",
            content="Test content",
            type="code",
            tags=["python", "test"],
        )

        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

            with patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()):
                service.memory_repo.create = AsyncMock(
                    return_value=MagicMock(id=uuid4(), scope="working")
                )

                response = await service.remember(
                    request=request,
                    user_id="test_user",
                )

                assert response.memory_id is not None

    @pytest.mark.asyncio
    async def test_remember_with_importance(self, service):
        """Test memory creation with custom importance."""
        request = RememberRequest(
            summary="Important memory",
            content="Critical content",
            type="decision",
            importance=0.9,
        )

        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

            with patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()):
                service.memory_repo.create = AsyncMock(
                    return_value=MagicMock(id=uuid4(), scope="working")
                )

                response = await service.remember(
                    request=request,
                    user_id="test_user",
                )

                assert response.memory_id is not None

    @pytest.mark.asyncio
    async def test_recall_basic(self, service):
        """Test basic memory recall."""
        request = RecallRequest(
            query="test query",
            k=5,
        )

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
        )

        # Check response
        assert response.results is not None
        assert len(response.results) > 0
        assert response.total > 0

    @pytest.mark.asyncio
    async def test_recall_with_filters(self, service):
        """Test recall with filters."""
        request = RecallRequest(
            query="test query",
            k=5,
            filters={"type": "code"},
        )

        service.search_service.hybrid_search = AsyncMock(return_value=[])

        await service.recall(
            request=request,
            user_id="test_user",
        )

        # Check filters were passed
        service.search_service.hybrid_search.assert_called_once()
        call_kwargs = service.search_service.hybrid_search.call_args.kwargs
        assert call_kwargs["filters"] == {"type": "code"}

    @pytest.mark.asyncio
    async def test_recall_no_results(self, service):
        """Test recall with no results."""
        request = RecallRequest(
            query="nonexistent query",
            k=5,
        )

        service.search_service.hybrid_search = AsyncMock(return_value=[])

        response = await service.recall(
            request=request,
            user_id="test_user",
        )

        assert response.total == 0
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_forget_by_id(self, service):
        """Test forgetting memory by ID."""
        memory_id = uuid4()
        request = ForgetRequest(memory_id=memory_id)

        # Mock memory exists
        service.memory_repo.get_by_id = AsyncMock(
            return_value=MagicMock(id=memory_id, user_id="test_user")
        )
        service.memory_repo.delete = AsyncMock()

        with patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()):
            response = await service.forget(
                request=request,
                user_id="test_user",
            )

            assert response.success is True
            assert response.deleted_count == 1

    @pytest.mark.asyncio
    async def test_forget_nonexistent(self, service):
        """Test forgetting nonexistent memory."""
        memory_id = uuid4()
        request = ForgetRequest(memory_id=memory_id)

        # Mock memory doesn't exist
        service.memory_repo.get_by_id = AsyncMock(return_value=None)

        from utils.exceptions import NotFoundException

        with pytest.raises(NotFoundException):
            await service.forget(
                request=request,
                user_id="test_user",
            )

    @pytest.mark.asyncio
    async def test_forget_by_query(self, service):
        """Test forgetting memories by query."""
        request = ForgetRequest(query="test query", k=10)

        # Mock search results
        search_results = [{"id": str(uuid4()), "score": 0.9, "payload": {}} for _ in range(3)]

        service.search_service.hybrid_search = AsyncMock(return_value=search_results)
        service.memory_repo.get_by_id = AsyncMock(return_value=MagicMock(user_id="test_user"))
        service.memory_repo.delete = AsyncMock()

        with patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()):
            response = await service.forget(
                request=request,
                user_id="test_user",
            )

            assert response.success is True
            assert response.deleted_count > 0

    @pytest.mark.asyncio
    async def test_reference_basic(self, service):
        """Test getting memory reference."""
        memory_id = uuid4()
        request = ReferenceRequest(memory_id=memory_id)

        # Mock memory exists
        mock_memory = MagicMock(
            id=memory_id,
            user_id="test_user",
            summary="Test",
            content="Test content",
            details={"key": "value"},
            type="code",
            created_at=datetime.utcnow(),
        )

        service.memory_repo.get_by_id = AsyncMock(return_value=mock_memory)

        response = await service.reference(
            request=request,
            user_id="test_user",
        )

        assert response.memory_id == memory_id
        assert response.summary is not None
        assert response.content is not None
        assert response.details is not None

    @pytest.mark.asyncio
    async def test_reference_nonexistent(self, service):
        """Test getting reference for nonexistent memory."""
        memory_id = uuid4()
        request = ReferenceRequest(memory_id=memory_id)

        service.memory_repo.get_by_id = AsyncMock(return_value=None)

        from utils.exceptions import NotFoundException

        with pytest.raises(NotFoundException):
            await service.reference(
                request=request,
                user_id="test_user",
            )

    @pytest.mark.asyncio
    async def test_remember_embedding_error(self, service):
        """Test handling of embedding generation errors."""
        request = RememberRequest(
            summary="Test",
            content="Test",
            type="code",
        )

        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(side_effect=Exception("Embedding failed"))

            with pytest.raises(Exception, match="Embedding failed"):
                await service.remember(
                    request=request,
                    user_id="test_user",
                )

    @pytest.mark.asyncio
    async def test_remember_qdrant_error(self, service):
        """Test handling of Qdrant errors."""
        request = RememberRequest(
            summary="Test",
            content="Test",
            type="code",
        )

        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

            with patch(
                "services.memory_service.add_memory_to_qdrant",
                new=AsyncMock(side_effect=Exception("Qdrant error")),
            ):
                with pytest.raises(Exception, match="Qdrant error"):
                    await service.remember(
                        request=request,
                        user_id="test_user",
                    )

    @pytest.mark.asyncio
    async def test_recall_with_rerank(self, service):
        """Test recall with reranking enabled."""
        request = RecallRequest(
            query="test query",
            k=5,
            use_rerank=True,
        )

        search_results = [
            {
                "id": str(uuid4()),
                "score": 0.9,
                "payload": {
                    "summary": "Test",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            }
        ]

        service.search_service.hybrid_search = AsyncMock(return_value=search_results)

        await service.recall(
            request=request,
            user_id="test_user",
        )

        # Reranking should be passed to search service
        call_kwargs = service.search_service.hybrid_search.call_args.kwargs
        assert "use_rerank" in call_kwargs

    @pytest.mark.asyncio
    async def test_remember_working_scope(self, service):
        """Test that new memories start in working scope."""
        request = RememberRequest(
            summary="New memory",
            content="Content",
            type="code",
        )

        with patch("services.memory_service.ensure_user_collection", new=AsyncMock()):
            service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

            with patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()):
                mock_memory = MagicMock(id=uuid4(), scope="working")
                service.memory_repo.create = AsyncMock(return_value=mock_memory)

                response = await service.remember(
                    request=request,
                    user_id="test_user",
                )

                # New memories should be in working scope
                assert response.scope == "working"

    @pytest.mark.asyncio
    async def test_recall_sorted_by_score(self, service):
        """Test that recall results are sorted by score."""
        request = RecallRequest(query="test", k=10)

        # Unsorted results
        search_results = [
            {
                "id": str(uuid4()),
                "score": 0.7,
                "payload": {
                    "summary": "Test",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            },
            {
                "id": str(uuid4()),
                "score": 0.9,
                "payload": {
                    "summary": "Test",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            },
            {
                "id": str(uuid4()),
                "score": 0.8,
                "payload": {
                    "summary": "Test",
                    "type": "code",
                    "created_at": datetime.utcnow().isoformat(),
                },
            },
        ]

        service.search_service.hybrid_search = AsyncMock(return_value=search_results)

        response = await service.recall(
            request=request,
            user_id="test_user",
        )

        # Results should be sorted by score descending
        scores = [r.score for r in response.results]
        assert scores == sorted(scores, reverse=True)
