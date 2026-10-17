"""#2015: recall's dense arm on a context with content chunk vectors.

Flag off: the dense search call is byte-identical to before (no
include_chunks kwarg, same limit). Flag on: the dense arm asks for chunk
points, oversamples, and collapses hits to one per memory before the merge;
the matched chunk text reaches the reranker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.reranker_service import RerankerService
from services.search_service import SearchService
from utils.content_chunks import CONTENT_CHUNK_DENSE_MAX_FETCH, CONTENT_CHUNK_DENSE_OVERSAMPLE

pytestmark = pytest.mark.usefixtures("no_identity_links")

_WS = "00000000-0000-0000-0000-000000000001"
_CTX = "00000000-0000-0000-0000-000000000002"


def _config(chunks):
    attrs = {
        "semantic_weight": 0.6,
        "bm25_weight": 0.4,
        "fetch_factor": 3,
        "use_rerank": False,
        "reranker_provider": "self_hosted",
        "reranker_model": None,
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 512,
    }
    if chunks is not None:
        attrs["content_chunks_enabled"] = chunks
    return type("Cfg", (), attrs)()


@pytest.fixture(autouse=True)
def _routing():
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


_DENSE = [
    {
        "id": "c1",
        "score": 0.95,
        "payload": {"summary": "a", "chunk_of": "m1", "chunk_text": "late", "chunk_index": 2},
        "embedding": [],
    },
    {"id": "m2", "score": 0.80, "payload": {"summary": "b"}, "embedding": []},
    {"id": "m1", "score": 0.40, "payload": {"summary": "a"}, "embedding": []},
]


async def _search(config, dense, search_mode="semantic", k=5):
    service = SearchService(MagicMock())
    service._get_search_config = AsyncMock(return_value=config)
    ctx_svc = MagicMock()
    ctx_svc.is_context_shared = AsyncMock(return_value=False)
    qdrant = AsyncMock(return_value=dense)
    with (
        patch("services.search_service.search_memories_qdrant", new=qdrant),
        patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])),
        patch("services.context_service.ContextService", return_value=ctx_svc),
    ):
        results = await service.hybrid_search(
            query="q",
            user_id="u",
            workspace_id=_WS,
            context_id=_CTX,
            k=k,
            search_mode=search_mode,
        )
    return results, qdrant.await_args.kwargs


@pytest.mark.parametrize("flag", [None, False])
async def test_flag_off_call_is_unchanged(flag):
    plain = [{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "embedding": []}]
    results, kwargs = await _search(_config(flag), plain)
    assert "include_chunks" not in kwargs
    assert kwargs["limit"] == 5 * 3
    assert [r["id"] for r in results] == ["m2"]


async def test_flag_on_oversamples_and_collapses_before_the_merge():
    results, kwargs = await _search(_config(True), _DENSE)
    assert kwargs["include_chunks"] is True
    assert kwargs["limit"] == 5 * 3 * CONTENT_CHUNK_DENSE_OVERSAMPLE
    assert [r["id"] for r in results] == ["m1", "m2"]
    assert results[0]["score"] == 0.95
    assert results[0]["semantic_score_raw"] == 0.95
    assert results[0]["matched_chunk_text"] == "late"


async def test_flag_on_hybrid_merges_one_row_per_memory():
    results, _ = await _search(_config(True), _DENSE, search_mode="hybrid")
    assert sorted(r["id"] for r in results) == ["m1", "m2"]
    m1 = next(r for r in results if r["id"] == "m1")
    assert m1["semantic_score_raw"] == 0.95
    assert m1["matched_chunk_text"] == "late"


async def test_oversampled_limit_is_capped():
    _, kwargs = await _search(_config(True), [], k=100)
    assert kwargs["limit"] == CONTENT_CHUNK_DENSE_MAX_FETCH


async def test_reranker_sees_the_matched_chunk_text():
    svc = RerankerService(MagicMock())
    provider = MagicMock()
    provider.rerank = AsyncMock(return_value=[{"index": 0, "relevance_score": 0.9}])
    svc.get_active_provider = AsyncMock(return_value=provider)
    candidates = [
        {
            "id": "m1",
            "payload": {"summary": "S", "context_summary": "C"},
            "matched_chunk_text": "the passage",
            "hybrid_score": 0.5,
        }
    ]
    await svc.rerank("q", candidates, k=1, user_id="u", context_id=_CTX, workspace_id=_WS)
    documents = provider.rerank.await_args.args[1]
    assert documents == ["S C\nthe passage"]
