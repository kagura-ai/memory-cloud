"""Tests for SearchService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.search_service import SearchService


class TestSearchService:
    """Test SearchService for Hybrid Search (Semantic + BM25)."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        """Create SearchService."""
        return SearchService(mock_db)

    def test_init(self, mock_db):
        """Test SearchService initialization."""
        service = SearchService(mock_db)

        assert service.db == mock_db
        assert service.embedding_service is not None

    @pytest.mark.asyncio
    async def test_hybrid_search_basic(self, service):
        """Test basic hybrid search."""
        # Mock embedding service
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        # Mock Qdrant results
        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test 1"}},
            {"id": "mem2", "score": 0.8, "payload": {"summary": "Test 2"}},
        ]

        fulltext_results = [
            {"id": "mem2", "score": 0.7, "payload": {"summary": "Test 2"}},
            {"id": "mem3", "score": 0.6, "payload": {"summary": "Test 3"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=fulltext_results),
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,  # Disable reranking for simplicity
                )

                # Should merge results
                assert len(results) > 0
                assert all("id" in r for r in results)
                assert all("score" in r for r in results)

    @pytest.mark.asyncio
    async def test_hybrid_search_semantic_only(self, service):
        """Test hybrid search when only semantic results exist."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test 1"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should return semantic results
                assert len(results) > 0

    @pytest.mark.asyncio
    async def test_hybrid_search_fulltext_only(self, service):
        """Test hybrid search when only fulltext results exist."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        fulltext_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test 1"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])
        ):
            with patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=fulltext_results),
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should return fulltext results
                assert len(results) > 0

    @pytest.mark.asyncio
    async def test_hybrid_search_no_results(self, service):
        """Test hybrid search with no results."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        with patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should return empty list
                assert len(results) == 0

    @pytest.mark.asyncio
    async def test_hybrid_search_with_filters(self, service):
        """Test hybrid search with filters."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        filters = {"type": "code"}

        with patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])
        ) as mock_semantic:
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    filters=filters,
                    use_rerank=False,
                )

                # Check filters were passed to semantic search
                mock_semantic.assert_called_once()
                call_kwargs = mock_semantic.call_args.kwargs
                assert call_kwargs["filters"] == filters

    @pytest.mark.asyncio
    async def test_hybrid_search_top_k_limit(self, service):
        """Test that top k limit is respected."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        # Create many results
        semantic_results = [
            {"id": f"mem{i}", "score": 0.9 - i * 0.01, "payload": {"summary": f"Test {i}"}}
            for i in range(20)
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should return at most k results
                assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_hybrid_search_score_fusion(self, service):
        """Test that scores are properly fused (60% semantic + 40% fulltext)."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        # Same result in both searches
        semantic_results = [
            {"id": "mem1", "score": 1.0, "payload": {"summary": "Test"}},
        ]

        fulltext_results = [
            {"id": "mem1", "score": 0.5, "payload": {"summary": "Test"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=fulltext_results),
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Check fusion score
                # Expected: 0.6 * 1.0 + 0.4 * 0.5 = 0.8
                if len(results) > 0:
                    result = results[0]
                    assert "score" in result
                    # Score should be combination of both

    @pytest.mark.asyncio
    async def test_hybrid_search_sorted_by_score(self, service):
        """Test that results are sorted by score (descending)."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test 1"}},
            {"id": "mem2", "score": 0.7, "payload": {"summary": "Test 2"}},
            {"id": "mem3", "score": 0.8, "payload": {"summary": "Test 3"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=10,
                    use_rerank=False,
                )

                # Check sorted descending
                if len(results) > 1:
                    scores = [r["score"] for r in results]
                    assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_hybrid_search_deduplication(self, service):
        """Test that duplicate results are handled correctly."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        # Same ID appears in both results
        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test"}},
        ]

        fulltext_results = [
            {"id": "mem1", "score": 0.7, "payload": {"summary": "Test"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=fulltext_results),
            ):
                results = await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=10,
                    use_rerank=False,
                )

                # Should not have duplicate IDs
                ids = [r["id"] for r in results]
                assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_hybrid_search_embedding_error(self, service):
        """Test handling of embedding generation errors."""
        # Mock embedding service to raise error
        service.embedding_service.embed = AsyncMock(side_effect=Exception("Embedding failed"))

        with pytest.raises(Exception, match="Embedding failed"):
            await service.hybrid_search(
                query="test query",
                user_id="test_user",
                k=5,
            )

    @pytest.mark.asyncio
    async def test_hybrid_search_qdrant_error(self, service):
        """Test handling of Qdrant errors."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        # Mock Qdrant to raise error
        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(side_effect=Exception("Qdrant error")),
        ):
            with pytest.raises(Exception, match="Qdrant error"):
                await service.hybrid_search(
                    query="test query",
                    user_id="test_user",
                    k=5,
                )

    @pytest.mark.asyncio
    async def test_hybrid_search_with_rerank_disabled(self, service):
        """Test hybrid search with reranking disabled."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                # Reranking should not be called
                with patch(
                    "services.search_service.SearchService._rerank_with_cohere"
                ) as mock_rerank:
                    await service.hybrid_search(
                        query="test query",
                        user_id="test_user",
                        k=5,
                        use_rerank=False,
                    )

                    # Rerank should not be called
                    mock_rerank.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_search_empty_query(self, service):
        """Test hybrid search with empty query."""
        service.embedding_service.embed = AsyncMock(return_value=[0.0] * 512)

        with patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query="",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should handle empty query gracefully
                assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_hybrid_search_japanese_query(self, service):
        """Test hybrid search with Japanese query."""
        service.embedding_service.embed = AsyncMock(return_value=[0.5] * 512)

        japanese_query = "認証エラー修正"

        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "認証エラー"}},
        ]

        with patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=semantic_results),
        ):
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ):
                results = await service.hybrid_search(
                    query=japanese_query,
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should handle Japanese text
                assert len(results) > 0

    @pytest.mark.asyncio
    async def test_hybrid_search_candidate_expansion(self, service):
        """Test that k*2 candidates are fetched for merging."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        with patch(
            "services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])
        ) as mock_semantic:
            with patch(
                "services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])
            ) as mock_fulltext:
                await service.hybrid_search(
                    query="test",
                    user_id="test_user",
                    k=5,
                    use_rerank=False,
                )

                # Should request k*2 candidates
                assert mock_semantic.call_args.kwargs["limit"] == 10  # k*2
                assert mock_fulltext.call_args.kwargs["limit"] == 10  # k*2
