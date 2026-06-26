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
