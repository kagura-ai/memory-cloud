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


async def _search(config, dense, search_mode="semantic", k=5, chunk_points=True):
    service = SearchService(MagicMock())
    service._get_search_config = AsyncMock(return_value=config)
    ctx_svc = MagicMock()
    ctx_svc.is_context_shared = AsyncMock(return_value=False)
    qdrant = AsyncMock(return_value=dense)
    with (
        patch("services.search_service.search_memories_qdrant", new=qdrant),
        patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])),
        patch("services.context_service.ContextService", return_value=ctx_svc),
        patch("services.search_service.chunk_points_supported", return_value=chunk_points),
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


async def test_flag_on_without_chunk_point_support_keeps_the_plain_call():
    """#2072: the LanceDB store has no chunk points, so a stored flag (e.g.
    from a Qdrant deployment) neither oversamples nor asks for chunks."""
    plain = [{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "embedding": []}]
    results, kwargs = await _search(_config(True), plain, chunk_points=False)
    assert "include_chunks" not in kwargs
    assert kwargs["limit"] == 5 * 3
    assert [r["id"] for r in results] == ["m2"]


async def test_flag_on_past_the_plans_cap_keeps_the_plain_call():
    """#2103: a stored true the plan does not allow (gated deployment, past
    the cap) neither oversamples nor asks for chunks."""
    plain = [{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "embedding": []}]
    effective = AsyncMock(return_value=False)
    with patch("services.search_service.content_chunks_effective", new=effective):
        results, kwargs = await _search(_config(True), plain)
    assert "include_chunks" not in kwargs
    assert kwargs["limit"] == 5 * 3
    assert effective.await_args.kwargs == {"stored": True}


async def test_flag_off_asks_the_plan_nothing():
    effective = AsyncMock(return_value=True)
    with patch("services.search_service.content_chunks_effective", new=effective):
        await _search(_config(False), [])
    effective.assert_not_awaited()


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


# #2015 PR 2a: cross-context recall follows each listed context's own flag.
_CTX2 = "00000000-0000-0000-0000-000000000003"


async def _search_contexts(config, dense, chunk_contexts, chunk_points=True):
    service = SearchService(MagicMock())
    service._get_search_config = AsyncMock(return_value=config)
    service._chunk_enabled_context_ids = AsyncMock(return_value=chunk_contexts)
    qdrant = AsyncMock(return_value=dense)
    with (
        patch("services.search_service.search_memories_qdrant", new=qdrant),
        patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])),
        patch("services.search_service.chunk_points_supported", return_value=chunk_points),
    ):
        results = await service.hybrid_search(
            query="q",
            user_id="u",
            workspace_id=_WS,
            context_id=[_CTX, _CTX2],
            k=5,
            search_mode="semantic",
        )
    if chunk_points:
        service._chunk_enabled_context_ids.assert_awaited_once_with([_CTX, _CTX2])
    else:
        service._chunk_enabled_context_ids.assert_not_awaited()
    return results, qdrant.await_args.kwargs


async def test_cross_context_recall_asks_nothing_about_the_primary_alone():
    """The per-context set decides; the primary's own flag is not read twice."""
    effective = AsyncMock(return_value=True)
    with patch("services.search_service.content_chunks_effective", new=effective):
        await _search_contexts(_config(True), [], [_CTX])
    effective.assert_not_awaited()


async def test_cross_context_primary_off_still_gets_an_opted_in_secondarys_chunks():
    results, kwargs = await _search_contexts(_config(False), _DENSE, [_CTX2])
    assert kwargs["include_chunks"] == [_CTX2]
    assert kwargs["limit"] == 5 * 3 * CONTENT_CHUNK_DENSE_OVERSAMPLE
    assert [r["id"] for r in results] == ["m1", "m2"]  # collapsed


async def test_cross_context_primary_on_admits_only_the_contexts_that_opted_in():
    _, kwargs = await _search_contexts(_config(True), [], [_CTX])
    assert kwargs["include_chunks"] == [_CTX]


async def test_cross_context_none_opted_in_is_the_summary_only_call():
    plain = [{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "embedding": []}]
    results, kwargs = await _search_contexts(_config(True), plain, [])
    assert "include_chunks" not in kwargs
    assert kwargs["limit"] == 5 * 3
    assert [r["id"] for r in results] == ["m2"]


async def test_cross_context_without_chunk_point_support_reads_no_flags():
    """#2072: on LanceDB every listed context is treated as flag-off, the
    opted-in ones included, and no flag lookup is made."""
    plain = [{"id": "m2", "score": 0.8, "payload": {"summary": "b"}, "embedding": []}]
    results, kwargs = await _search_contexts(
        _config(True), plain, [_CTX, _CTX2], chunk_points=False
    )
    assert "include_chunks" not in kwargs
    assert kwargs["limit"] == 5 * 3
    assert [r["id"] for r in results] == ["m2"]


async def test_chunk_enabled_context_ids_keeps_the_callers_spelling_and_order():
    from uuid import UUID

    result = MagicMock()
    result.scalars.return_value.all.return_value = [UUID(_CTX2)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.begin_nested = MagicMock(return_value=_Savepoint())
    ids = await SearchService(db)._chunk_enabled_context_ids([_CTX, _CTX2])
    assert ids == [_CTX2]
    compiled = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "content_chunks_enabled IS true" in compiled


class _Savepoint:
    """``db.begin_nested()``: records whether it was rolled back."""

    def __init__(self):
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.rolled_back = exc_type is not None
        return False


async def test_chunk_enabled_context_ids_fails_closed_inside_a_savepoint():
    """A failed lookup must not leave the recall's transaction aborted."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    savepoint = _Savepoint()
    db.begin_nested = MagicMock(return_value=savepoint)
    assert await SearchService(db)._chunk_enabled_context_ids([_CTX, _CTX2]) == []
    assert savepoint.rolled_back


async def test_keyword_cross_context_search_reads_no_chunk_flags():
    service = SearchService(MagicMock())
    service._get_search_config = AsyncMock(return_value=_config(True))
    service._chunk_enabled_context_ids = AsyncMock(return_value=[_CTX])
    with patch("services.search_service.search_memories_fulltext", new=AsyncMock(return_value=[])):
        await service.hybrid_search(
            query="q",
            user_id="u",
            workspace_id=_WS,
            context_id=[_CTX, _CTX2],
            k=5,
            search_mode="keyword",
        )
    service._chunk_enabled_context_ids.assert_not_awaited()


async def test_chunk_enabled_context_ids_asks_the_policy_for_active_ones():
    """#2103: cross-context recall keeps only contexts within their plan's cap."""
    from uuid import UUID

    db = MagicMock()
    db.begin_nested = MagicMock(return_value=_Savepoint())
    active = AsyncMock(return_value={UUID(_CTX)})
    with patch("services.search_service.content_chunks_effective_ids", new=active):
        ids = await SearchService(db)._chunk_enabled_context_ids([_CTX, _CTX2])
    assert ids == [_CTX]
    assert active.await_args.args[1] == [UUID(_CTX), UUID(_CTX2)]
