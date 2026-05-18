"""Tests for SearchService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.search_service import SearchService


@pytest.fixture
def _patch_routing():
    """Patch ``resolve_routing_from_config`` so tests don't hit DB.

    #475 PR-3: production code calls ``embed_with_usage`` on the routed
    ``embed_svc`` (returns a ``(vector, tokens)`` tuple). ``tokens=0``
    exercises the cache-hit branch — the writer is never constructed,
    so these tests stay focused on search behavior without needing
    ``LLMCallLogWriter`` mocks.

    Module-level so both ``TestSearchService`` and ``TestSearchMode`` share
    a single source of truth — a signature drift here previously required
    editing two identical copies.
    """
    with patch(
        "services.search_service.resolve_routing_from_config",
        return_value=(
            "kagura_memories",
            MagicMock(
                embed_with_usage=AsyncMock(return_value=([0.1] * 512, 0)),
                provider="openai",
                model="text-embedding-3-small",
            ),
        ),
    ):
        yield


class TestSearchService:
    """Test SearchService for Hybrid Search (Semantic + BM25)."""

    @pytest.fixture(autouse=True)
    def _routing(self, _patch_routing):
        """Activate the module-level routing patch for every test in this class."""

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
    def _routing(self, _patch_routing):
        """Activate the module-level routing patch for every test in this class."""

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
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)
        return mock_ctx_svc

    @pytest.mark.asyncio
    async def test_keyword_mode_skips_embedding(self, service, default_search_config):
        """keyword mode skips Qdrant (no embedding call needed).

        The "no embedding call" half of the contract is asserted at the
        routed-service level in ``test_recall_llm_instrumentation.py``
        (``test_keyword_only_search_skips_embedding_and_writer``) — that
        is where the production code actually obtains its embedding
        service, and where the assertion remains meaningful post-#475 PR-3.
        """
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


class TestSharedContextReadFlag:
    """Test ``is_shared_context_read`` flag behavior in ``hybrid_search`` (#708 F2).

    The flag controls an authorization-sensitive bypass and the Qdrant
    user filter, so its semantics must be directly verified at the
    SearchService layer (not only at the MemoryService.recall caller).
    """

    @pytest.fixture(autouse=True)
    def _routing(self, _patch_routing):
        """Activate the module-level routing patch for every test in this class."""

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

    @pytest.mark.asyncio
    async def test_flag_true_skips_permission_probe_but_still_checks_sharing(
        self, service, default_search_config
    ):
        """``is_shared_context_read=True`` MUST skip ONLY the workspace-member
        probe, NOT the ``is_context_shared`` probe.

        Loop 5 fix (Copilot): conflating the two probes was the security
        bug. The handler-layer ``_resolve_context_for_read`` is the
        authoritative access check, so the workspace-member probe is
        redundant. But ``is_context_shared`` controls the Qdrant user_id
        filter and remains essential — a private context's filter must
        stay applied even when the caller's access has been verified at
        a higher layer.
        """
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        # Shared context — so is_shared_context=True is the expected outcome.
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=True)
        mock_perm_svc = MagicMock()
        mock_perm_svc.is_workspace_member = AsyncMock(return_value=False)

        with (
            patch(
                "services.search_service.search_memories_qdrant",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "services.search_service.search_memories_fulltext",
                new=AsyncMock(return_value=[]),
            ),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
            patch("services.permission_service.PermissionService", return_value=mock_perm_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="caller_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                is_shared_context_read=True,
            )

        # ``is_context_shared`` MUST run — drives the user_id filter decision.
        mock_ctx_svc.is_context_shared.assert_called_once()
        # ``is_workspace_member`` MUST be skipped — handler already verified.
        mock_perm_svc.is_workspace_member.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_true_shared_context_propagates_to_both_qdrant_calls(
        self, service, default_search_config
    ):
        """``is_shared_context_read=True`` + shared context MUST forward
        ``is_shared_context=True`` to BOTH the semantic and keyword Qdrant
        call sites. Either site missing propagation re-introduces the
        ``user_id == caller`` filter and silently drops source memories.
        """
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=True)
        mock_qdrant = AsyncMock(return_value=[])
        mock_fulltext = AsyncMock(return_value=[])

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="caller_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                is_shared_context_read=True,
            )

        assert mock_qdrant.call_args.kwargs["is_shared_context"] is True, (
            "Semantic Qdrant call MUST receive is_shared_context=True for shared contexts"
        )
        assert mock_fulltext.call_args.kwargs["is_shared_context"] is True, (
            "Keyword Qdrant call MUST receive is_shared_context=True for shared contexts"
        )

    @pytest.mark.asyncio
    async def test_flag_true_private_context_keeps_user_id_filter(
        self, service, default_search_config
    ):
        """#708 loop 5 security fence: PRIVATE context + cross-workspace caller
        MUST keep the Qdrant ``user_id == caller`` filter.

        Scenario: a creator owns a now-private context in workspace W2, but
        is reading it from their active workspace W1 (cross-workspace under
        Option A). Without this fence, ``is_shared_context_read=True``
        would drop the user_id filter and return memories authored by
        OTHER users (e.g. memories written before the context was made
        private), bypassing the private-context access rule.

        ``is_shared_context`` is derived from the context's privacy state,
        NOT from the cross-workspace flag.
        """
        service._get_search_config = AsyncMock(return_value=default_search_config)

        mock_ctx_svc = MagicMock()
        # Private context — is_shared_context=False is the expected outcome.
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)
        mock_qdrant = AsyncMock(return_value=[])
        mock_fulltext = AsyncMock(return_value=[])

        with (
            patch("services.search_service.search_memories_qdrant", new=mock_qdrant),
            patch("services.search_service.search_memories_fulltext", new=mock_fulltext),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="caller_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                is_shared_context_read=True,  # Cross-workspace, BUT context is private
            )

        assert mock_qdrant.call_args.kwargs["is_shared_context"] is False, (
            "Private context MUST keep user_id filter even under Option A cross-workspace read"
        )
        assert mock_fulltext.call_args.kwargs["is_shared_context"] is False, (
            "Private context MUST keep user_id filter on keyword path too"
        )

    @pytest.mark.asyncio
    async def test_flag_false_runs_legacy_probe(self, service, default_search_config):
        """Default (flag=False): legacy ``is_context_shared`` probe still runs.

        Regression fence: the bypass MUST be opt-in. Same-workspace reads
        (the dominant path) must continue to call ``is_context_shared``
        + ``is_workspace_member`` so callers who legitimately need the
        check still get it. Without this fence, any operator who flips
        the default to True would silently disable the legacy
        authorization probe for every recall.
        """
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
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="caller_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                # is_shared_context_read omitted (defaults to False)
            )

        mock_ctx_svc.is_context_shared.assert_called_once()

    @pytest.mark.asyncio
    async def test_flag_true_propagates_disallow_env_fallback_to_embed(
        self, service, default_search_config
    ):
        """#708 loop 7: ``is_shared_context_read=True`` MUST forward
        ``disallow_env_fallback=True`` to ``embed_svc.embed_with_usage``.

        Without this, a TOCTOU race between the H1 preflight
        ``has_byok_key`` probe in ``MemoryService.recall`` and the actual
        ``_get_user_api_key`` call here could route Option A reads
        through ``OPENAI_API_KEY`` env fallback — bypassing the BYOK-only
        spend cap (PR #711) the H1 gate is meant to enforce.
        """
        service._get_search_config = AsyncMock(return_value=default_search_config)

        # Capture the kwargs embed_with_usage receives.
        embed_calls: list[dict] = []

        async def _capture_embed(*args, **kwargs):
            embed_calls.append(kwargs)
            return ([0.1] * 512, 10)  # (vector, tokens)

        mock_embed_svc = MagicMock()
        mock_embed_svc.embed_with_usage = _capture_embed
        mock_embed_svc.provider = "openai"
        mock_embed_svc.model = "text-embedding-3-small"
        mock_embed_svc.resolve_paid_by = AsyncMock(return_value="byok")

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=True)

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
                "services.search_service.resolve_routing_from_config",
                return_value=("kagura_memories", mock_embed_svc),
            ),
            patch("services.context_service.ContextService", return_value=mock_ctx_svc),
        ):
            await service.hybrid_search(
                query="test",
                user_id="caller_user",
                workspace_id="00000000-0000-0000-0000-000000000001",
                context_id="00000000-0000-0000-0000-000000000002",
                k=5,
                is_shared_context_read=True,
            )

        # Exactly one embed call (semantic path), and it received the flag.
        assert len(embed_calls) == 1
        assert embed_calls[0].get("disallow_env_fallback") is True, (
            "Option A reads MUST forward disallow_env_fallback=True"
        )
