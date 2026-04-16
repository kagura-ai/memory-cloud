"""Tests for SearchService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.search_service import SearchService


class TestSearchService:
    """Test SearchService for Hybrid Search (Semantic + BM25)."""

    @pytest.fixture(autouse=True)
    def _patch_routing(self):
        """Patch resolve_routing_from_config so tests don't hit DB."""
        with patch(
            "services.search_service.resolve_routing_from_config",
            return_value=(
                "kagura_memories",
                MagicMock(embed=AsyncMock(return_value=[0.1] * 512)),
            ),
        ):
            yield

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        """Create SearchService."""
        return SearchService(mock_db)

    @pytest.fixture
    def default_search_config(self):
        """Create default search config mock."""
        return type(
            "DefaultConfig",
            (),
            {
                "semantic_weight": 0.6,
                "bm25_weight": 0.4,
                "fetch_factor": 3,
                "use_rerank": False,
                "embedding_model": "text-embedding-3-small",
                "embedding_dimensions": 512,
            },
        )()

    def test_init(self, mock_db):
        """Test SearchService initialization."""
        service = SearchService(mock_db)
        assert service.db == mock_db
        assert service.embedding_service is not None

    @pytest.mark.asyncio
    async def test_hybrid_search_basic(self, service, default_search_config):
        """Test basic hybrid search with all deps mocked."""
        semantic_results = [
            {"id": "mem1", "score": 0.9, "payload": {"summary": "Test 1"}},
            {"id": "mem2", "score": 0.8, "payload": {"summary": "Test 2"}},
        ]
        fulltext_results = [
            {"id": "mem2", "score": 0.7, "payload": {"summary": "Test 2"}},
            {"id": "mem3", "score": 0.6, "payload": {"summary": "Test 3"}},
        ]

        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)

        with (
            patch(
                "services.search_service.search_memories_qdrant",
                new=AsyncMock(return_value=semantic_results),
            ),
            patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=fulltext_results),
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_ctx_svc,
            ),
        ):
            results = await service.hybrid_search(
                query="test query",
                user_id="test_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                use_rerank=False,
            )
            assert len(results) > 0
            assert all("id" in r for r in results)

    @pytest.mark.asyncio
    async def test_hybrid_search_no_results(self, service, default_search_config):
        """Test hybrid search with no results."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)

        with (
            patch(
                "services.search_service.search_memories_qdrant",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "services.context_service.ContextService",
                return_value=mock_ctx_svc,
            ),
        ):
            results = await service.hybrid_search(
                query="test query",
                user_id="test_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                use_rerank=False,
            )
            assert len(results) == 0


class TestSearchMode:
    """Test search_mode parameter (Issue #17)."""

    @pytest.fixture(autouse=True)
    def _patch_routing(self):
        """Patch resolve_routing_from_config so tests don't hit DB."""
        with patch(
            "services.search_service.resolve_routing_from_config",
            return_value=(
                "kagura_memories",
                MagicMock(embed=AsyncMock(return_value=[0.1] * 512)),
            ),
        ):
            yield

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return SearchService(mock_db)

    @pytest.fixture
    def default_search_config(self):
        return type(
            "DefaultConfig",
            (),
            {
                "semantic_weight": 0.6,
                "bm25_weight": 0.4,
                "fetch_factor": 3,
                "use_rerank": False,
                "embedding_model": "text-embedding-3-small",
                "embedding_dimensions": 512,
            },
        )()

    def _setup_mocks(self, service, default_search_config):
        """Common mock setup for search mode tests."""
        service.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)
        return mock_ctx_svc

    @pytest.mark.asyncio
    async def test_keyword_mode_skips_embedding(self, service, default_search_config):
        """keyword mode should not call embed() or search_memories_qdrant."""
        mock_ctx_svc = self._setup_mocks(service, default_search_config)
        fulltext_results = [{"id": "mem1", "score": 10.0, "payload": {"summary": "Test"}}]

        mock_qdrant = AsyncMock(return_value=[])
        mock_fulltext = AsyncMock(return_value=fulltext_results)

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            results = await service.hybrid_search(
                query="test",
                user_id="user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                search_mode="keyword",
            )
            mock_qdrant.assert_not_called()
            mock_fulltext.assert_called_once()
            service.embedding_service.embed.assert_not_called()
            assert len(results) == 1
            assert results[0]["id"] == "mem1"

    @pytest.mark.asyncio
    async def test_semantic_mode_skips_fulltext(self, service, default_search_config):
        """semantic mode should not call search_memories_fulltext."""
        mock_ctx_svc = self._setup_mocks(service, default_search_config)
        semantic_results = [{"id": "mem1", "score": 0.9, "payload": {"summary": "Test"}}]

        mock_qdrant = AsyncMock(return_value=semantic_results)
        mock_fulltext = AsyncMock(return_value=[])

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            results = await service.hybrid_search(
                query="test",
                user_id="user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                search_mode="semantic",
            )
            mock_qdrant.assert_called_once()
            mock_fulltext.assert_not_called()
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_hybrid_mode_calls_both(self, service, default_search_config):
        """hybrid mode should call both backends and merge."""
        mock_ctx_svc = self._setup_mocks(service, default_search_config)

        mock_qdrant = AsyncMock(return_value=[{"id": "mem1", "score": 0.9, "payload": {}}])
        mock_fulltext = AsyncMock(return_value=[{"id": "mem2", "score": 0.8, "payload": {}}])

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            results = await service.hybrid_search(
                query="test",
                user_id="user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                search_mode="hybrid",
            )
            mock_qdrant.assert_called_once()
            mock_fulltext.assert_called_once()
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_default_mode_is_hybrid(self, service, default_search_config):
        """Default search_mode should be hybrid (both backends called)."""
        mock_ctx_svc = self._setup_mocks(service, default_search_config)

        mock_qdrant = AsyncMock(return_value=[])
        mock_fulltext = AsyncMock(return_value=[])

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
            )
            mock_qdrant.assert_called_once()
            mock_fulltext.assert_called_once()


class TestSearchServiceMergeResults:
    """Test _merge_results method directly — no external deps needed."""

    @pytest.fixture
    def service(self):
        return SearchService(MagicMock())

    def test_merge_empty_both(self, service):
        """Test merge with no results from either source."""
        assert service._merge_results([], []) == []

    def test_merge_semantic_only(self, service):
        """Test merge with only semantic results."""
        semantic = [{"id": "m1", "score": 0.9, "payload": {}}]
        result = service._merge_results(semantic, [])
        assert len(result) == 1
        assert result[0]["semantic_score"] == 1.0
        assert result[0]["keyword_score"] == 0.0

    def test_merge_keyword_only(self, service):
        """Test merge with only keyword results."""
        keyword = [{"id": "m1", "score": 0.8, "payload": {}}]
        result = service._merge_results([], keyword)
        assert len(result) == 1
        assert result[0]["semantic_score"] == 0.0
        assert result[0]["keyword_score"] == 1.0

    def test_merge_overlap(self, service):
        """Test merge with overlapping results."""
        semantic = [{"id": "m1", "score": 1.0, "payload": {}}]
        keyword = [{"id": "m1", "score": 0.5, "payload": {}}]
        result = service._merge_results(semantic, keyword)
        assert len(result) == 1
        assert result[0]["hybrid_score"] == pytest.approx(1.0)

    def test_merge_sorted_by_hybrid_score(self, service):
        """Test that merge results are sorted by hybrid_score descending."""
        semantic = [
            {"id": "m1", "score": 0.5, "payload": {}},
            {"id": "m2", "score": 1.0, "payload": {}},
        ]
        keyword = [{"id": "m3", "score": 1.0, "payload": {}}]
        result = service._merge_results(semantic, keyword)
        scores = [r["hybrid_score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_merge_custom_weights(self, service):
        """Test merge with custom weights."""
        semantic = [{"id": "m1", "score": 1.0, "payload": {}}]
        keyword = [{"id": "m2", "score": 1.0, "payload": {}}]
        result = service._merge_results(semantic, keyword, semantic_weight=0.8, keyword_weight=0.2)
        m1 = next(r for r in result if r["id"] == "m1")
        m2 = next(r for r in result if r["id"] == "m2")
        assert m1["hybrid_score"] == pytest.approx(0.8)
        assert m2["hybrid_score"] == pytest.approx(0.2)

    def test_merge_score_normalization(self, service):
        """Test that scores are normalized to 0-1 range."""
        semantic = [
            {"id": "m1", "score": 0.5, "payload": {}},
            {"id": "m2", "score": 1.0, "payload": {}},
        ]
        result = service._merge_results(semantic, [])
        m2 = next(r for r in result if r["id"] == "m2")
        m1 = next(r for r in result if r["id"] == "m1")
        assert m2["semantic_score"] == 1.0
        assert m1["semantic_score"] == 0.5

    def test_merge_deduplication(self, service):
        """Test that duplicate IDs are merged, not duplicated."""
        semantic = [{"id": "m1", "score": 0.9, "payload": {"summary": "A"}}]
        keyword = [{"id": "m1", "score": 0.7, "payload": {"summary": "A"}}]
        result = service._merge_results(semantic, keyword)
        assert len(result) == 1

    def test_merge_many_results_sorted(self, service):
        """Test merge with many results maintains sort order."""
        semantic = [{"id": f"m{i}", "score": 0.9 - i * 0.01, "payload": {}} for i in range(20)]
        result = service._merge_results(semantic, [])
        scores = [r["hybrid_score"] for r in result]
        assert scores == sorted(scores, reverse=True)
