"""#2958: ``HYBRID_FUSION`` selects how a hybrid search fuses its two legs.

- ``weighted`` (the default): today's fusion, byte for byte — each leg divided
  by its top score, ``hybrid_score = sem * w_s + kw * w_k``.
- ``rrf``: plain reciprocal rank fusion (k=60), scaled into (0, 1] so a row
  first in both legs scores 1.0; the context's weights are not used.
- ``keyword_floor``: the weighted order with the keyword leg interleaved 1:1
  (weighted first, each memory once), so keyword hit j sits at position <= 2j;
  the scores are made non-increasing so a later stable re-sort keeps it.

With ``rrf`` / ``keyword_floor`` and the reranker on, the rerank pool is drawn
from both legs; with ``weighted`` it is the merged list cut to ``fetch_size``.
Every mode stamps ``semantic_rank`` / ``keyword_rank`` on the rows.

House style follows test_search_service_rerank_gate.py: Qdrant/BM25/
ContextService patched, ``_get_search_config`` replaced with a duck-typed
config object, ``get_settings`` replaced with a fresh ``Settings``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from config.settings import Settings
from services.search_service import SearchService
from utils.exceptions import ExternalServiceError

pytestmark = pytest.mark.usefixtures("no_identity_links")

_WS = "00000000-0000-0000-0000-000000000001"
_CTX = "00000000-0000-0000-0000-000000000002"

_ENV = [
    "ENABLE_RERANKING",
    "HYBRID_FUSION",
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


def _sem(mid: str, score: float) -> dict:
    return {"id": mid, "score": score, "payload": {"summary": mid}}


# Hand-built legs. The semantic leg ranks A..F; the keyword leg ranks K first,
# a keyword-only hit that the weighted fusion buries (0.4 < every semantic row
# at 0.6 * ~0.9), then A (in both legs) and Z (keyword-only, weak).
_SEMANTIC = [
    _sem("A", 0.90),
    _sem("B", 0.88),
    _sem("C", 0.86),
    _sem("D", 0.84),
    _sem("E", 0.82),
    _sem("F", 0.80),
]
_KEYWORD = [_sem("K", 10.0), _sem("A", 5.0), _sem("Z", 4.0)]


def _config(use_rerank: bool = False, *, fetch_factor: int = 3):
    return type(
        "Cfg",
        (),
        {
            "semantic_weight": 0.6,
            "bm25_weight": 0.4,
            "fetch_factor": fetch_factor,
            "use_rerank": use_rerank,
            "reranker_provider": "self_hosted",
            "reranker_model": None,
            "embedding_model": "text-embedding-3-small",
            "embedding_dimensions": 512,
        },
    )()


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


@pytest.fixture
def use_fusion(monkeypatch):
    """``use_fusion("rrf")`` → a fresh Settings with that HYBRID_FUSION in place."""
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)

    def _apply(fusion: str | None = None) -> Settings:
        s = (
            Settings(_env_file=None)
            if fusion is None
            else Settings(_env_file=None, hybrid_fusion=fusion)
        )
        monkeypatch.setattr("services.search_service.get_settings", lambda: s)
        monkeypatch.setattr("config.settings.get_settings", lambda: s)
        return s

    return _apply


def _service() -> SearchService:
    svc = SearchService(MagicMock())
    svc.reranker_service = MagicMock()

    async def _no_provider(*, candidates, outcome=None, **_kwargs):
        # The no-provider path: the candidates come back untouched.
        if outcome is not None:
            outcome["scored"] = False
        return candidates

    svc.reranker_service.rerank = AsyncMock(side_effect=_no_provider)
    return svc


async def _search(
    service: SearchService,
    *,
    k: int,
    config=None,
    semantic=None,
    keyword=None,
    semantic_error: Exception | None = None,
    use_rerank: bool | None = None,
) -> list[dict]:
    service._get_search_config = AsyncMock(return_value=config or _config())
    ctx_svc = MagicMock()
    ctx_svc.is_context_shared = AsyncMock(return_value=False)
    quota = MagicMock()
    quota.check_feature_access = AsyncMock(return_value=(True, None))
    qdrant = (
        AsyncMock(side_effect=semantic_error)
        if semantic_error is not None
        else AsyncMock(return_value=[dict(r) for r in (semantic or _SEMANTIC)])
    )
    with (
        patch("services.search_service.search_memories_qdrant", new=qdrant),
        patch(
            "services.search_service.search_memories_fulltext",
            new=AsyncMock(return_value=[dict(r) for r in (keyword or _KEYWORD)]),
        ),
        patch("services.context_service.ContextService", return_value=ctx_svc),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        return await service.hybrid_search(
            query="q",
            user_id="u",
            workspace_id=_WS,
            context_id=_CTX,
            k=k,
            use_rerank=use_rerank,
        )


def _ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows]


_RANK_KEYS = ("semantic_rank", "keyword_rank")


def _without_ranks(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in r.items() if key not in _RANK_KEYS} for r in rows]


# --- the setting -------------------------------------------------------------------


class TestSetting:
    def test_weighted_is_the_default(self, monkeypatch):
        monkeypatch.delenv("HYBRID_FUSION", raising=False)
        assert Settings(_env_file=None).hybrid_fusion == "weighted"

    @pytest.mark.parametrize(
        ("raw", "value"),
        [
            (" RRF ", "rrf"),
            ("Keyword_Floor", "keyword_floor"),
            ("", "weighted"),
            ("  ", "weighted"),
        ],
    )
    def test_value_is_trimmed_and_lowercased_and_blank_is_weighted(self, monkeypatch, raw, value):
        monkeypatch.setenv("HYBRID_FUSION", raw)
        assert Settings(_env_file=None).hybrid_fusion == value

    @pytest.mark.parametrize("raw", ["union", "rrf,weighted", "keyword-floor"])
    def test_unknown_hybrid_fusion_fails_settings(self, monkeypatch, raw):
        monkeypatch.setenv("HYBRID_FUSION", raw)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


# --- weighted: today's fusion ------------------------------------------------------


class TestWeighted:
    async def test_weighted_is_the_default_and_byte_identical(self, use_fusion):
        """The default answers exactly what ``_merge_results`` answers today."""
        use_fusion()
        service = _service()
        rows = await _search(service, k=8)

        expected = service._merge_results(
            [dict(r) for r in _SEMANTIC], [dict(r) for r in _KEYWORD], 0.6, 0.4
        )
        assert _without_ranks(rows) == expected
        # Pinned by hand as well, so a change to _merge_results cannot hide here.
        assert _ids(rows) == ["A", "B", "C", "D", "E", "F", "K", "Z"]
        assert rows[0]["hybrid_score"] == pytest.approx(0.6 + 0.4 * 0.5)
        assert rows[6]["hybrid_score"] == pytest.approx(0.4)
        assert rows[7]["hybrid_score"] == pytest.approx(0.4 * 0.4)

    async def test_weighted_buries_the_keyword_only_hit(self, use_fusion):
        use_fusion("weighted")
        rows = await _search(_service(), k=3)
        assert _ids(rows) == ["A", "B", "C"]

    async def test_rerank_pool_unchanged_for_weighted(self, use_fusion):
        """k=1, fetch_factor 3 doubled for rerank → fetch_size 6: the pool is the
        weighted list cut to 6, so K and Z (positions 7 and 8) never reach it."""
        use_fusion("weighted")
        service = _service()
        await _search(service, k=1, config=_config(True), use_rerank=None)
        candidates = service.reranker_service.rerank.await_args.kwargs["candidates"]
        assert _ids(candidates) == ["A", "B", "C", "D", "E", "F"]


# --- rrf ---------------------------------------------------------------------------


class TestRRF:
    async def test_rrf_scores_rank_based_and_top_both_legs_is_1(self, use_fusion):
        use_fusion("rrf")
        rows = await _search(_service(), k=8)
        by_id = {r["id"]: r for r in rows}
        scale = 61 / 2
        assert by_id["A"]["hybrid_score"] == pytest.approx((1 / 61 + 1 / 62) * scale)
        assert by_id["K"]["hybrid_score"] == pytest.approx((1 / 61) * scale)
        assert by_id["B"]["hybrid_score"] == pytest.approx((1 / 62) * scale)
        assert by_id["Z"]["hybrid_score"] == pytest.approx((1 / 63) * scale)
        # K (keyword rank 1) beats B (semantic rank 2): the buried hit surfaces.
        # C and Z tie (rank 3 of one leg each); the weighted order breaks it.
        assert _ids(rows) == ["A", "K", "B", "C", "Z", "D", "E", "F"]

    async def test_rrf_first_in_both_legs_scores_exactly_one(self, use_fusion):
        use_fusion("rrf")
        rows = await _search(
            _service(), k=3, semantic=[_sem("A", 0.9), _sem("B", 0.5)], keyword=[_sem("A", 3.0)]
        )
        assert rows[0]["id"] == "A"
        assert rows[0]["hybrid_score"] == pytest.approx(1.0)

    async def test_rrf_scores_lie_in_the_unit_interval(self, use_fusion):
        use_fusion("rrf")
        semantic = [_sem(f"s{i}", 1.0 - i / 400) for i in range(200)]
        keyword = [_sem(f"k{i}", 200.0 - i) for i in range(200)]
        rows = await _search(_service(), k=400, semantic=semantic, keyword=keyword)
        assert len(rows) == 400
        assert all(0.0 < r["hybrid_score"] <= 1.0 for r in rows)

    async def test_rrf_ignores_the_context_weights(self, use_fusion):
        use_fusion("rrf")
        heavy = _config()
        heavy.semantic_weight, heavy.bm25_weight = 0.95, 0.05
        rows = await _search(_service(), k=8, config=heavy)
        assert _ids(rows) == ["A", "K", "B", "C", "Z", "D", "E", "F"]

    async def test_rrf_keeps_semantic_score_raw_and_score(self, use_fusion):
        use_fusion("rrf")
        rows = await _search(_service(), k=8)
        by_id = {r["id"]: r for r in rows}
        assert by_id["A"]["semantic_score_raw"] == pytest.approx(0.90)
        assert by_id["K"]["semantic_score_raw"] is None
        # ``score`` / the leg scores are those _merge_results gives (normalised legs).
        assert by_id["A"]["semantic_score"] == pytest.approx(1.0)
        assert by_id["A"]["keyword_score"] == pytest.approx(0.5)
        assert by_id["K"]["keyword_score"] == pytest.approx(1.0)
        assert by_id["K"]["score"] == pytest.approx(1.0)
        assert by_id["B"]["score"] == pytest.approx(0.88 / 0.90)


# --- keyword_floor -----------------------------------------------------------------


class TestKeywordFloor:
    async def test_keyword_floor_places_keyword_rank_j_within_2j(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        assert _ids(rows) == ["A", "K", "B", "C", "Z", "D", "E", "F"]
        position = {r["id"]: i + 1 for i, r in enumerate(rows)}
        for j, hit in enumerate(_KEYWORD, start=1):
            assert position[hit["id"]] <= 2 * j, hit["id"]

    async def test_keyword_floor_surfaces_the_buried_hit_at_small_k(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=3)
        assert _ids(rows) == ["A", "K", "B"]

    async def test_keyword_floor_scores_non_increasing(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        scores = [r["hybrid_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)
        by_id = {r["id"]: r for r in rows}
        # Promoted rows are lifted to a tie with the row after them; nothing is lowered.
        assert by_id["K"]["hybrid_score"] == pytest.approx(by_id["B"]["hybrid_score"])
        assert by_id["Z"]["hybrid_score"] == pytest.approx(by_id["D"]["hybrid_score"])
        assert by_id["A"]["hybrid_score"] == pytest.approx(0.6 + 0.4 * 0.5)
        assert by_id["B"]["hybrid_score"] == pytest.approx(0.6 * 0.88 / 0.90)

    async def test_keyword_floor_each_memory_once(self, use_fusion):
        use_fusion("keyword_floor")
        keyword = [_sem("A", 9.0), _sem("B", 8.0), _sem("K", 7.0), _sem("A", 1.0)]
        rows = await _search(_service(), k=20, keyword=keyword)
        assert len(_ids(rows)) == len(set(_ids(rows)))
        assert set(_ids(rows)) == {"A", "B", "C", "D", "E", "F", "K"}

    async def test_reinforce_resort_keeps_keyword_floor(self, use_fusion):
        """MemoryService's reinforce re-sort (on by default since #1207) sorts by
        ``hybrid_score * factor``; with equal factors the floor must survive it."""
        from services.memory_service import MemoryService

        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        floor_order = _ids(rows)

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        created = datetime(2020, 1, 1)
        memories = {
            mid: MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=created)
            for mid in floor_order
        }
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as repo,
            patch("services.feedback_service.FeedbackService") as feedback,
        ):
            repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            feedback.return_value.aggregate_for_memories = AsyncMock(return_value={})
            await svc._maybe_reinforce_rerank(rows, memories, uuid4(), top_k=8)
        assert _ids(rows) == floor_order


# --- the rerank pool ---------------------------------------------------------------


class TestRerankPool:
    @pytest.mark.parametrize("fusion", ["rrf", "keyword_floor"])
    async def test_rerank_pool_draws_both_legs_when_not_weighted(self, use_fusion, fusion):
        """fetch_size 6: the top 3 of each leg (A B C ∪ K A Z) in fused order,
        filled up to 6 from the fused order."""
        use_fusion(fusion)
        service = _service()
        await _search(service, k=1, config=_config(True), use_rerank=None)
        candidates = service.reranker_service.rerank.await_args.kwargs["candidates"]
        ids = _ids(candidates)
        assert len(ids) == 6
        assert {"K", "Z"} <= set(ids)
        assert {"A", "B", "C"} <= set(ids)
        assert ids == ["A", "K", "B", "C", "Z", "D"]

    @pytest.mark.parametrize("fusion", ["rrf", "keyword_floor"])
    async def test_rerank_pool_never_exceeds_fetch_size(self, use_fusion, fusion):
        use_fusion(fusion)
        service = _service()
        semantic = [_sem(f"s{i}", 1.0 - i / 100) for i in range(7)]
        keyword = [_sem(f"k{i}", 10.0 - i) for i in range(7)]
        await _search(
            service,
            k=1,
            config=_config(True, fetch_factor=3),
            semantic=semantic,
            keyword=keyword,
            use_rerank=None,
        )
        candidates = service.reranker_service.rerank.await_args.kwargs["candidates"]
        ids = _ids(candidates)
        assert len(ids) == 6
        assert set(ids) == {"s0", "s1", "s2", "k0", "k1", "k2"}

    @pytest.mark.parametrize("fusion", ["rrf", "keyword_floor"])
    async def test_rerank_failure_answers_from_the_full_fused_list(self, use_fusion, fusion):
        use_fusion(fusion)
        service = _service()
        service.reranker_service.rerank = AsyncMock(side_effect=RuntimeError("down"))
        rows = await _search(service, k=8, config=_config(True), use_rerank=None)
        assert _ids(rows) == ["A", "K", "B", "C", "Z", "D", "E", "F"]


# --- per-leg ranks -----------------------------------------------------------------


class TestLegRanks:
    @pytest.mark.parametrize("fusion", ["weighted", "rrf", "keyword_floor"])
    async def test_leg_ranks_stamped_in_every_mode(self, use_fusion, fusion):
        use_fusion(fusion)
        rows = await _search(_service(), k=8)
        ranks = {r["id"]: (r["semantic_rank"], r["keyword_rank"]) for r in rows}
        assert ranks == {
            "A": (1, 2),
            "B": (2, None),
            "C": (3, None),
            "D": (4, None),
            "E": (5, None),
            "F": (6, None),
            "K": (None, 1),
            "Z": (None, 3),
        }

    @pytest.mark.parametrize("fusion", ["weighted", "rrf", "keyword_floor"])
    async def test_leg_ranks_stamped_on_degrade(self, use_fusion, fusion):
        """#1515: the semantic arm fails → keyword-only, the same in every mode."""
        use_fusion(fusion)
        rows = await _search(_service(), k=8, semantic_error=ExternalServiceError("down"))
        assert _ids(rows) == ["K", "A", "Z"]
        assert [(r["semantic_rank"], r["keyword_rank"]) for r in rows] == [
            (None, 1),
            (None, 2),
            (None, 3),
        ]
        assert [r["score"] for r in rows] == [10.0, 5.0, 4.0]

    async def test_leg_ranks_survive_a_rerank(self, use_fusion):
        use_fusion("rrf")
        service = _service()

        async def _reverse(*, candidates, outcome=None, k, **_kwargs):
            if outcome is not None:
                outcome["scored"] = True
            for i, c in enumerate(candidates):
                c["hybrid_score"] = c["rerank_score"] = i / 10
            return list(reversed(candidates))[:k]

        service.reranker_service.rerank = AsyncMock(side_effect=_reverse)
        rows = await _search(service, k=2, config=_config(True), use_rerank=None)
        assert all("semantic_rank" in r and "keyword_rank" in r for r in rows)
