"""#2958: ``HYBRID_FUSION`` selects how a hybrid search fuses its two legs.

- ``weighted`` (the default): today's fusion, byte for byte — each leg divided
  by its top score, ``hybrid_score = sem * w_s + kw * w_k``.
- ``rrf``: plain reciprocal rank fusion (k=60), scaled into (0, 1] so a row
  first in both legs scores 1.0; the context's weights are not used.
- ``keyword_floor``: the weighted scores, with the keyword leg interleaved 1:1
  into the order (weighted first, each memory once), so keyword hit j sits at
  position <= 2j. No score changes; recall re-applies the floor after its
  re-sorts unless a reranker scored the list.

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
from services.search_service import KEYWORD_FLOOR_KEY, SearchService, keyword_floor_order
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

    async def test_keyword_floor_changes_no_score(self, use_fusion):
        """The floor is an ordering step only: every row keeps the weighted fused
        score, so the API score, the Hebbian activation, the related-index seeds
        and the supersede fold read a keyword hit's real strength."""
        use_fusion("weighted")
        weighted = {r["id"]: r["hybrid_score"] for r in await _search(_service(), k=8)}
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        assert {r["id"]: r["hybrid_score"] for r in rows} == weighted
        by_id = {r["id"]: r for r in rows}
        # K is promoted to position 2 and still scores what the weighted fusion gives it.
        assert _ids(rows)[1] == "K"
        assert by_id["K"]["hybrid_score"] == pytest.approx(0.4)
        assert by_id["Z"]["hybrid_score"] == pytest.approx(0.4 * 0.4)

    async def test_keyword_floor_marks_the_rows_for_the_recall_step(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        assert all(r[KEYWORD_FLOOR_KEY] is True for r in rows)

    @pytest.mark.parametrize("fusion", ["weighted", "rrf"])
    async def test_other_modes_leave_no_floor_mark(self, use_fusion, fusion):
        use_fusion(fusion)
        rows = await _search(_service(), k=8)
        assert not any(KEYWORD_FLOOR_KEY in r for r in rows)

    async def test_no_floor_mark_on_a_degraded_search(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8, semantic_error=ExternalServiceError("down"))
        assert not any(KEYWORD_FLOOR_KEY in r for r in rows)

    async def test_a_scored_rerank_is_final_and_leaves_no_floor_mark(self, use_fusion):
        use_fusion("keyword_floor")
        service = _service()

        async def _scored(*, candidates, outcome=None, k, **_kwargs):
            if outcome is not None:
                outcome["scored"] = True
            for i, c in enumerate(candidates):
                c["hybrid_score"] = c["rerank_score"] = 1.0 - i / 100
            return list(candidates)[:k]

        service.reranker_service.rerank = AsyncMock(side_effect=_scored)
        rows = await _search(service, k=4, config=_config(True), use_rerank=None)
        assert rows and not any(KEYWORD_FLOOR_KEY in r for r in rows)

    async def test_a_failed_rerank_keeps_the_floor_mark(self, use_fusion):
        use_fusion("keyword_floor")
        service = _service()
        service.reranker_service.rerank = AsyncMock(side_effect=RuntimeError("down"))
        rows = await _search(service, k=8, config=_config(True), use_rerank=None)
        assert all(r[KEYWORD_FLOOR_KEY] is True for r in rows)

    async def test_keyword_floor_each_memory_once(self, use_fusion):
        use_fusion("keyword_floor")
        keyword = [_sem("A", 9.0), _sem("B", 8.0), _sem("K", 7.0), _sem("A", 1.0)]
        rows = await _search(_service(), k=20, keyword=keyword)
        assert len(_ids(rows)) == len(set(_ids(rows)))
        assert set(_ids(rows)) == {"A", "B", "C", "D", "E", "F", "K"}

    async def test_reinforce_resort_keeps_keyword_floor(self, use_fusion):
        """Unequal reinforce factors: B (heavily adopted, helpful) is boosted
        past the promoted keyword-only K. The recall's floor step, after the
        re-sorts, puts keyword hit j back at position <= 2j."""
        from services.feedback_service import FeedbackAggregate
        from services.memory_service import MemoryService

        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)

        svc = MemoryService(MagicMock())
        cfg = MagicMock(
            reinforce_enabled=True,
            reinforce_max_boost=Decimal("0.15"),
            reinforce_require_host_arbitration=False,
        )
        created = datetime(2020, 1, 1)
        memories = {
            mid: MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=created)
            for mid in _ids(rows)
        }
        memories["B"].reference_count = 10
        memories["B"].importance = 0.9
        boosted = str(memories["B"].id)
        with (
            patch("repositories.config_repository.ContextSearchConfigRepository") as repo,
            patch("services.feedback_service.FeedbackService") as feedback,
        ):
            repo.return_value.get_by_context = AsyncMock(return_value=cfg)
            feedback.return_value.aggregate_for_memories = AsyncMock(
                return_value={
                    boosted: FeedbackAggregate(
                        memory_id=boosted, helpful_count=5, not_helpful_count=0
                    )
                }
            )
            await svc._maybe_reinforce_rerank(rows, memories, uuid4(), top_k=8)
        # The re-sort alone buries K again (score x factor).
        assert _ids(rows).index("K") > 1
        rows[:] = keyword_floor_order(rows)
        position = {r["id"]: i + 1 for i, r in enumerate(rows)}
        for j, hit in enumerate(_KEYWORD, start=1):
            assert position[hit["id"]] <= 2 * j, hit["id"]
        # The reinforce order is the base the keyword hits are interleaved into.
        assert _ids(rows)[:3] == ["A", "K", "B"]
        assert rows[1]["hybrid_score"] == pytest.approx(0.4)

    async def test_floor_step_is_idempotent_without_a_re_sort(self, use_fusion):
        use_fusion("keyword_floor")
        rows = await _search(_service(), k=8)
        before = _ids(rows)
        assert _ids(keyword_floor_order(rows)) == before

    def test_recall_applies_the_floor_after_the_re_sorts(self):
        """The floor is the last ordering step of recall before the selection
        plan and the page cut: after reinforce and graph boost."""
        import inspect

        from services.memory_service import MemoryService

        src = inspect.getsource(MemoryService.recall)
        graph_boost = src.index("await self._maybe_graph_boost(")
        floor = src.index("keyword_floor_order(search_results)")
        selection = src.index("await self._recall_apply_selection_plan(")
        build = src.index("await self._recall_build_responses(")
        assert src.index("await self._maybe_reinforce_rerank(") < graph_boost < floor
        assert floor < selection < build


# --- the rerank pool ---------------------------------------------------------------


class TestRerankPool:
    async def test_an_unknown_fusion_value_takes_the_weighted_pool(self, monkeypatch):
        """A settings object without a real HYBRID_FUSION (a MagicMock in older
        tests) must not take the both-legs pool: only rrf / keyword_floor do."""
        settings = MagicMock(enable_reranking=True)
        monkeypatch.setattr("services.search_service.get_settings", lambda: settings)
        service = _service()
        await _search(service, k=1, config=_config(True), use_rerank=None)
        candidates = service.reranker_service.rerank.await_args.kwargs["candidates"]
        assert _ids(candidates) == ["A", "B", "C", "D", "E", "F"]

    @pytest.mark.parametrize("fusion", ["rrf", "keyword_floor"])
    async def test_rerank_pool_draws_both_legs_when_not_weighted(self, use_fusion, fusion):
        """fetch_size 6: the fused head (A K B), the keyword leg's top 3 not yet
        in (Z), filled up to 6 from the fused order (C D), in fused order."""
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
    async def test_rerank_pool_holds_the_fused_head_small(self, use_fusion, fusion):
        """k=1, fetch_size 4: x is third in both legs, so in neither leg's top 2,
        yet first in the fused order. The pool holds the fused head."""
        use_fusion(fusion)
        service = _service()
        await _search(
            service,
            k=1,
            config=_config(True, fetch_factor=2),
            semantic=[_sem("a", 0.9), _sem("b", 0.8), _sem("x", 0.7)],
            keyword=[_sem("c", 10.0), _sem("d", 9.0), _sem("x", 8.0)],
            use_rerank=None,
        )
        ids = _ids(service.reranker_service.rerank.await_args.kwargs["candidates"])
        assert len(ids) == 4
        assert "x" in ids
        assert {"c", "d"} <= set(ids)

    @pytest.mark.parametrize("fusion", ["rrf", "keyword_floor"])
    async def test_rerank_pool_holds_the_fused_head_large(self, use_fusion, fusion):
        """k=5, fetch_size 40: M is rank 21 in both legs (outside the top 20 of
        each) and first in the fused order."""
        use_fusion(fusion)
        service = _service()
        semantic = [_sem("M" if i == 21 else f"s{i}", 1.0 - i / 100) for i in range(1, 41)]
        keyword = [_sem("M" if i == 21 else f"k{i}", 100.0 - i) for i in range(1, 41)]
        await _search(
            service,
            k=5,
            config=_config(True, fetch_factor=4),
            semantic=semantic,
            keyword=keyword,
            use_rerank=None,
        )
        ids = _ids(service.reranker_service.rerank.await_args.kwargs["candidates"])
        assert len(ids) == 40
        assert "M" in ids
        assert {f"k{i}" for i in range(1, 21)} <= set(ids)

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
