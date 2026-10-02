"""#1784: the private-recall vector filter matches the caller's link set.

Pure unit tests of the two filter builders and of what recall hands them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from qdrant_client.models import MatchAny, MatchValue

from db.lance_store import build_lance_filter
from db.qdrant import _build_search_filter, _owner_ids_kwarg

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
ME = "local:admin"
OTHER = "google-oauth2|123"


def _user_condition(qdrant_filter):
    return next(c for c in qdrant_filter.must if c.key == "user_id")


class TestQdrantFilter:
    def test_no_links_is_the_single_author_filter(self):
        condition = _user_condition(_build_search_filter(WS, CTX, ME))

        assert condition.match == MatchValue(value=ME)

    def test_a_link_set_of_one_is_the_single_author_filter(self):
        condition = _user_condition(_build_search_filter(WS, CTX, ME, owner_ids=[ME]))

        assert condition.match == MatchValue(value=ME)

    def test_linked_authors_match_any_and_always_include_the_caller(self):
        condition = _user_condition(_build_search_filter(WS, CTX, ME, owner_ids=[OTHER]))

        assert condition.match == MatchAny(any=sorted([ME, OTHER]))

    def test_a_shared_context_has_no_author_filter_at_all(self):
        qdrant_filter = _build_search_filter(
            WS, CTX, ME, is_shared_context=True, owner_ids=[ME, OTHER]
        )

        assert all(c.key != "user_id" for c in qdrant_filter.must)

    def test_the_store_kwarg_is_absent_without_links(self):
        assert _owner_ids_kwarg(None) == {}
        assert _owner_ids_kwarg([]) == {}
        assert _owner_ids_kwarg([ME, OTHER]) == {"owner_ids": [ME, OTHER]}


class TestLanceFilter:
    def test_no_links_is_the_single_author_filter(self):
        assert f"user_id = '{ME}'" in build_lance_filter(WS, CTX, ME)

    def test_linked_authors_become_an_in_list(self):
        where = build_lance_filter(WS, CTX, ME, owner_ids=[OTHER, ME])

        assert f"user_id IN ('{OTHER}', '{ME}')" in where or (
            f"user_id IN ('{ME}', '{OTHER}')" in where
        )
        assert "user_id =" not in where

    def test_ids_are_quoted(self):
        where = build_lance_filter(WS, CTX, ME, owner_ids=["o'brien"])

        assert "'o''brien'" in where

    def test_a_shared_context_has_no_author_filter(self):
        where = build_lance_filter(WS, CTX, ME, is_shared_context=True, owner_ids=[ME, OTHER])

        assert "user_id" not in where


class TestRecallPassesTheLinkSet:
    """``SearchService`` resolves the link set for a private context only."""

    @staticmethod
    async def _recall(*, shared: bool, linked: frozenset[str]):
        from services.search_service import SearchService

        service = SearchService(MagicMock())
        service._get_search_config = AsyncMock(
            return_value=SimpleNamespace(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=3,
                use_rerank=False,
                embedding_model="text-embedding-3-small",
                embedding_dimensions=512,
            )
        )
        context_service = MagicMock()
        context_service.is_context_shared = AsyncMock(return_value=shared)
        fulltext = AsyncMock(return_value=[])
        resolve = AsyncMock(return_value=linked)
        with (
            patch("services.search_service.search_memories_qdrant", new=AsyncMock(return_value=[])),
            patch("services.search_service.search_memories_fulltext", new=fulltext),
            patch("services.search_service.linked_user_ids", new=resolve),
            patch("services.context_service.ContextService", return_value=context_service),
            patch.object(SearchService, "_verify_workspace_membership", AsyncMock(), create=True),
        ):
            await service.hybrid_search(
                query="anything",
                user_id=ME,
                workspace_id=WS,
                context_id=CTX,
                k=5,
                search_mode="keyword",
            )
        return fulltext, resolve

    @pytest.mark.asyncio
    async def test_private_context_with_links_passes_owner_ids(self):
        fulltext, _ = await self._recall(shared=False, linked=frozenset({ME, OTHER}))

        assert fulltext.await_args.kwargs["owner_ids"] == sorted([ME, OTHER])

    @pytest.mark.asyncio
    async def test_no_links_passes_nothing_extra(self):
        fulltext, _ = await self._recall(shared=False, linked=frozenset({ME}))

        fulltext.assert_awaited_once()
        assert "owner_ids" not in fulltext.await_args.kwargs
