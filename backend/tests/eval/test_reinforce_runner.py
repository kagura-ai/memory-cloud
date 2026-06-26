"""DB-free orchestration tests for the #1069 reinforce ON/OFF live runner.

Pins the control flow (seed adoption → score OFF → flip reinforce ON → score ON →
gate) and the per-arm scoring (population split + zero-adoption surfacing) with
fakes, so it runs in the plain unit suite — no live stack, no embeddings. The
gate decision math itself is pinned separately in ``test_reinforce_gate.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tests.eval import reinforce_runner
from tests.eval.reinforce_gate import POPULATION_CURRENT_FACT, POPULATION_RARE, ArmBlock
from tests.eval.tools.corpus import Corpus, Document, Query


class _Hit:
    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id


class _Resp:
    def __init__(self, ids: list[str]) -> None:
        self.results = [_Hit(i) for i in ids]


def _corpus_and_idmap():
    """The real reinforce corpus + a synthetic doc→memory id_map."""
    corpus = reinforce_runner._load_reinforce_corpus()
    # id_map is memory_id(str) -> doc_id; give every doc a stable synthetic uuid.
    doc_to_mem = {d.id: str(uuid4()) for d in corpus.documents}
    id_map = {mem: doc for doc, mem in doc_to_mem.items()}
    return corpus, id_map, doc_to_mem


class TestScoreReinforceArm:
    async def test_perfect_ranking_splits_populations_and_counts_zero_adoption(self):
        corpus, id_map, doc_to_mem = _corpus_and_idmap()
        adopted_docs = set(corpus.meta["adopted_docs"])
        gold_by_text = {q.text: q.relevant for q in corpus.queries}

        async def _recall(*, request, user_id, current_context_id, current_workspace_id):
            # Perfect retrieval: return each query's gold docs (as memory ids) first.
            ids = [doc_to_mem[d] for d in gold_by_text[request.query]]
            return _Resp(ids)

        svc = MagicMock()
        svc.recall = AsyncMock(side_effect=_recall)

        arm = await reinforce_runner._score_reinforce_arm(
            svc, corpus, id_map, "owner", uuid4(), uuid4(), adopted_docs
        )
        assert isinstance(arm, ArmBlock)
        # Both populations are perfectly retrieved.
        assert arm.populations[POPULATION_CURRENT_FACT]["mrr@10"] == 1.0
        assert arm.populations[POPULATION_RARE]["mrr@10"] == 1.0
        assert arm.populations[POPULATION_CURRENT_FACT]["n"] == 5
        assert arm.populations[POPULATION_RARE]["n"] == 5
        # 5 current-fact golds are adopted, 5 rare golds are zero-adoption → half
        # the 10 surfaced slots are zero-adoption.
        assert arm.zero_adoption_surfacing_rate == 0.5


class TestSeedAdoption:
    async def test_drives_reference_and_feedback_per_doc(self):
        svc = MagicMock()
        svc.reference = AsyncMock()
        svc.db = MagicMock()
        svc.db.commit = AsyncMock()
        mem_ids = [uuid4(), uuid4()]
        with patch("services.feedback_service.FeedbackService") as FB:
            FB.return_value.record_feedback = AsyncMock()
            await reinforce_runner._seed_adoption(
                svc, uuid4(), "owner", mem_ids, references=5, helpful=3
            )
        assert svc.reference.await_count == 2 * 5  # references per adopted doc
        assert FB.return_value.record_feedback.await_count == 2 * 3  # helpful per doc


class TestRunReinforceArms:
    async def test_off_scored_before_reinforce_flipped_on(self, monkeypatch):
        corpus, id_map, _ = _corpus_and_idmap()
        order: list[str] = []

        off_arm = ArmBlock(
            populations={
                POPULATION_CURRENT_FACT: {"n": 5, "p@5": 0.6, "mrr@10": 0.70, "ndcg@10": 0.6},
                POPULATION_RARE: {"n": 5, "p@5": 0.8, "mrr@10": 0.80, "ndcg@10": 0.8},
            },
            zero_adoption_surfacing_rate=0.50,
        )
        on_arm = ArmBlock(
            populations={
                POPULATION_CURRENT_FACT: {"n": 5, "p@5": 0.8, "mrr@10": 0.88, "ndcg@10": 0.8},
                POPULATION_RARE: {"n": 5, "p@5": 0.8, "mrr@10": 0.80, "ndcg@10": 0.8},
            },
            zero_adoption_surfacing_rate=0.49,
        )
        arms = iter((off_arm, on_arm))

        async def _seed(*a, **k):
            order.append("seed")

        async def _set(db, ctx_id, *, enabled, max_boost):
            order.append(f"set_reinforce={enabled}")

        async def _score(*a, **k):
            order.append("score")
            return next(arms)

        monkeypatch.setattr(reinforce_runner, "_seed_adoption", _seed)
        monkeypatch.setattr(reinforce_runner, "_set_reinforce", _set)
        monkeypatch.setattr(reinforce_runner, "_score_reinforce_arm", _score)

        svc = MagicMock()
        svc.db = MagicMock()
        results = await reinforce_runner._run_reinforce_arms(
            svc, corpus, id_map, "owner", uuid4(), uuid4(), "2026-06-26", write=False
        )

        # Seed first, OFF arm scored while reinforce is still off, THEN flip on,
        # THEN score the ON arm — the A/B is honest only in this order.
        assert order == ["seed", "score", "set_reinforce=True", "score"]
        assert results["gate"]["passed"] is True
        assert results["off"]["zero_adoption_surfacing_rate"] == 0.50
        assert results["on"]["populations"][POPULATION_CURRENT_FACT]["mrr@10"] == 0.88
        assert results["experiment"] == "reinforce_on_vs_off"
        # #1084: the OFF/ON arms genuinely differ (off_arm != on_arm), so the
        # vacuous-pass diagnostic is present and false.
        assert results["off_on_arms_identical"] is False

    async def test_identical_arms_flagged_for_vacuous_pass(self, monkeypatch):
        # #1084: a neutered toggle yields ON == OFF. The run still completes (a
        # no-headroom corpus can also tie) but flags off_on_arms_identical so the
        # operator does not graduate on a vacuous pass.
        corpus, id_map, _ = _corpus_and_idmap()
        same = ArmBlock(
            populations={
                POPULATION_CURRENT_FACT: {"n": 5, "p@5": 0.7, "mrr@10": 0.70, "ndcg@10": 0.7},
                POPULATION_RARE: {"n": 5, "p@5": 0.8, "mrr@10": 0.80, "ndcg@10": 0.8},
            },
            zero_adoption_surfacing_rate=0.50,
        )
        monkeypatch.setattr(reinforce_runner, "_seed_adoption", AsyncMock())
        monkeypatch.setattr(reinforce_runner, "_set_reinforce", AsyncMock())
        monkeypatch.setattr(reinforce_runner, "_score_reinforce_arm", AsyncMock(return_value=same))
        svc = MagicMock()
        svc.db = MagicMock()
        results = await reinforce_runner._run_reinforce_arms(
            svc, corpus, id_map, "owner", uuid4(), uuid4(), "2026-06-26", write=False
        )
        assert results["off_on_arms_identical"] is True
        assert results["gate"]["current_fact"]["improved"] is False

    async def test_unresolved_adopted_doc_fails_fast(self):
        # #1084: a typo'd adopted doc id was silently dropped (un-seeded), quietly
        # understating uplift. It must now fail fast like the population check.
        corpus = Corpus(
            meta={"adopted_docs": ["d1", "doc_typo"]},  # doc_typo is not in id_map
            documents=(Document(id="d1", source="memory", text="x" * 20),),
            queries=(
                Query(
                    id="q_cf",
                    bucket="memory-only",
                    text="q",
                    relevant=("d1",),
                    population="current_fact",
                ),
                Query(
                    id="q_rare", bucket="memory-only", text="q", relevant=("d1",), population="rare"
                ),
            ),
        )
        svc = MagicMock()
        with pytest.raises(RuntimeError, match="adopted_docs"):
            await reinforce_runner._run_reinforce_arms(
                svc, corpus, {str(uuid4()): "d1"}, "owner", uuid4(), uuid4(), "2026-06-26", False
            )

    async def test_typod_population_fails_fast(self):
        # A query with an unrecognized population would otherwise be silently
        # dropped from the gate's sample — the runner must reject it loudly.
        corpus = Corpus(
            meta={"adopted_docs": ["d1"]},
            documents=(Document(id="d1", source="memory", text="x" * 20),),
            queries=(
                Query(
                    id="q1",
                    bucket="memory-only",
                    text="q",
                    relevant=("d1",),
                    population="current-fact",
                ),  # hyphen typo, not current_fact
            ),
        )
        svc = MagicMock()
        with pytest.raises(RuntimeError, match="population"):
            await reinforce_runner._run_reinforce_arms(
                svc, corpus, {str(uuid4()): "d1"}, "owner", uuid4(), uuid4(), "2026-06-26", False
            )


class TestToggleChangesRanking:
    """#1084: the eval's OFF/ON arms are only meaningful if the real
    reinforce_enabled toggle actually reorders recall. The orchestration tests
    above mock _score_reinforce_arm, so this drives the REAL
    MemoryService._maybe_reinforce_rerank on a seeded-corpus-shaped candidate set
    and asserts OFF != ON — a regression that neuters the toggle fails here
    instead of letting the gate pass vacuously."""

    async def test_off_and_on_orderings_differ_under_real_toggle(self):
        from datetime import datetime
        from decimal import Decimal

        from services.feedback_service import FeedbackAggregate
        from services.memory_service import MemoryService

        old = datetime(2020, 1, 1)
        # Mirrors a current_fact probe: an adopted+helpful canonical with a LOWER
        # raw score than a non-adopted near-duplicate. Reinforce must lift it.
        canon = MagicMock(id=uuid4(), reference_count=8, importance=0.9, created_at=old)
        altdup = MagicMock(id=uuid4(), reference_count=0, importance=0.5, created_at=old)
        memories = {"canon": canon, "alt": altdup}

        async def _order(enabled: bool) -> list[str]:
            svc = MemoryService(MagicMock())
            cfg = MagicMock(
                reinforce_enabled=enabled,
                reinforce_max_boost=Decimal("0.15"),
                reinforce_require_host_arbitration=False,
            )
            sr = [{"id": "alt", "hybrid_score": 0.85}, {"id": "canon", "hybrid_score": 0.80}]
            with (
                patch("repositories.config_repository.ContextSearchConfigRepository") as Repo,
                patch("services.feedback_service.FeedbackService") as FB,
            ):
                Repo.return_value.get_by_context = AsyncMock(return_value=cfg)
                FB.return_value.aggregate_for_memories = AsyncMock(
                    return_value={
                        str(canon.id): FeedbackAggregate(
                            memory_id=str(canon.id), helpful_count=5, not_helpful_count=0
                        )
                    }
                )
                await svc._maybe_reinforce_rerank(sr, memories, uuid4(), top_k=10)
            return [r["id"] for r in sr]

        off_order = await _order(enabled=False)
        on_order = await _order(enabled=True)
        assert off_order == ["alt", "canon"]  # OFF: raw hybrid order preserved
        assert on_order == ["canon", "alt"]  # ON: adopted+helpful canonical rises
        assert off_order != on_order  # the toggle MUST change ranking
