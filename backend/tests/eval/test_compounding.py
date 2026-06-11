"""Deterministic tests for the #969 compounding-eval pure layer.

Covers the replay-plan builder, the graph-lane recovery metric, and the
cold→warm lift computation — everything in ``tests.eval.compounding`` that
needs no infrastructure. The live cold→replay→warm orchestration is exercised
by ``test_compounding_live.py`` (skip-guarded behind ``KAGURA_EVAL_LIVE=1``).
"""

from __future__ import annotations

import pytest

from tests.eval.compounding import (
    MODE_EXCLUDE_PROBES,
    MODE_INCLUDE_PROBES,
    PairAudit,
    build_replay_plan,
    classify_pair,
    compute_lift,
    recall_at_k,
    summarize_gate_audit,
)
from tests.eval.tools.corpus import load_corpus

# ---------------------------------------------------------------------------
# build_replay_plan
# ---------------------------------------------------------------------------


def test_probes_are_exactly_the_multi_gold_queries():
    """Every query with >= 2 gold docs is a probe; single-gold queries are not."""
    corpus = load_corpus()
    plan = build_replay_plan(corpus)

    expected_probe_ids = [q.id for q in corpus.queries if len(q.relevant) >= 2]
    assert [p.query_id for p in plan.probes] == expected_probe_ids
    # The v1 corpus pins this to the 5 cross-source queries — if the corpus
    # changes shape, the experiment design needs re-review, so fail loud.
    assert len(plan.probes) == 5
    assert all(p.bucket == "cross-source" for p in plan.probes)


def test_probe_seed_and_companions_partition_the_gold_set():
    corpus = load_corpus()
    plan = build_replay_plan(corpus)
    by_id = {q.id: q for q in corpus.queries}

    for probe in plan.probes:
        gold = by_id[probe.query_id].relevant
        assert probe.seed_doc == gold[0]
        assert list(probe.companion_docs) == list(gold[1:])
        assert probe.seed_doc not in probe.companion_docs


def test_exclude_probes_mode_replays_only_non_probe_queries():
    corpus = load_corpus()
    plan = build_replay_plan(corpus, mode=MODE_EXCLUDE_PROBES)

    probe_ids = {p.query_id for p in plan.probes}
    assert plan.mode == MODE_EXCLUDE_PROBES
    assert probe_ids.isdisjoint(plan.replay_query_ids)
    assert set(plan.replay_query_ids) == {q.id for q in corpus.queries if q.id not in probe_ids}


def test_include_probes_mode_replays_every_query():
    corpus = load_corpus()
    plan = build_replay_plan(corpus, mode=MODE_INCLUDE_PROBES)

    assert plan.mode == MODE_INCLUDE_PROBES
    assert list(plan.replay_query_ids) == [q.id for q in corpus.queries]


def test_plan_is_deterministic():
    corpus = load_corpus()
    a = build_replay_plan(corpus, mode=MODE_EXCLUDE_PROBES, rounds=3)
    b = build_replay_plan(corpus, mode=MODE_EXCLUDE_PROBES, rounds=3)
    assert a == b


def test_plan_rejects_bad_inputs():
    corpus = load_corpus()
    with pytest.raises(ValueError):
        build_replay_plan(corpus, mode="warm_and_fuzzy")
    with pytest.raises(ValueError):
        build_replay_plan(corpus, rounds=0)


# ---------------------------------------------------------------------------
# recall_at_k (companion recovery)
# ---------------------------------------------------------------------------


def test_recall_at_k_counts_relevant_in_window():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"b", "d"}, k=2) == 0.5
    assert recall_at_k(ranked, {"b", "d"}, k=4) == 1.0
    assert recall_at_k(ranked, {"z"}, k=4) == 0.0


def test_recall_at_k_empty_ranking_is_zero():
    assert recall_at_k([], {"a"}, k=5) == 0.0


def test_recall_at_k_rejects_empty_relevant_set():
    """An empty gold set would silently score 0/0 — fail loud instead."""
    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), k=5)


# ---------------------------------------------------------------------------
# compute_lift
# ---------------------------------------------------------------------------


def test_compute_lift_reports_absolute_and_relative_deltas():
    cold = {"recovery@10": 0.2, "mrr@10": 0.5}
    warm = {"recovery@10": 0.6, "mrr@10": 0.5}
    lift = compute_lift(cold, warm)

    assert lift["recovery@10"] == {
        "cold": 0.2,
        "warm": 0.6,
        "abs": 0.4,
        "rel": 2.0,
    }
    assert lift["mrr@10"]["abs"] == 0.0
    assert lift["mrr@10"]["rel"] == 0.0


def test_compute_lift_zero_cold_baseline_has_null_relative():
    """Cold-start graphs legitimately measure 0.0 — relative lift is undefined,
    not infinite, and must serialize as null rather than raising."""
    lift = compute_lift({"recovery@10": 0.0}, {"recovery@10": 0.4})
    assert lift["recovery@10"]["abs"] == 0.4
    assert lift["recovery@10"]["rel"] is None


def test_compute_lift_skips_non_metric_keys():
    """Counters like ``n`` and non-numeric metadata are not lift metrics."""
    cold = {"n": 5, "p@5": 0.5, "note": "cold"}
    warm = {"n": 5, "p@5": 0.7, "note": "warm"}
    lift = compute_lift(cold, warm)
    assert set(lift) == {"p@5"}


def test_compute_lift_requires_matching_metric_keys():
    """A cold/warm pair measured with different metric sets is a protocol bug."""
    with pytest.raises(ValueError):
        compute_lift({"p@5": 0.5}, {"p@10": 0.5})


# ---------------------------------------------------------------------------
# gate audit (classify_pair / summarize_gate_audit)
# ---------------------------------------------------------------------------
#
# Empirical finding from the first live run (2026-06-10): replay produced
# almost no Hebbian edges because (a) the semantic gate requires pair cosine
# >= min_similarity_for_edge and (b) a first update below prune_threshold is
# deleted, never accumulated. The audit makes a zero-lift result attributable
# instead of mute, so its classification logic must be exact.


def test_classify_pair_forms_when_both_gates_pass():
    assert classify_pair(0.6, 0.02, min_similarity=0.5, prune_threshold=0.01) == "forms"


def test_classify_pair_gated_by_cosine():
    assert classify_pair(0.398, 0.0366, min_similarity=0.5, prune_threshold=0.01) == "gated_cosine"


def test_classify_pair_below_prune_never_accumulates():
    assert classify_pair(0.6, 0.005, min_similarity=0.5, prune_threshold=0.01) == "below_prune"


def test_classify_pair_both_gates():
    assert (
        classify_pair(0.3, 0.005, min_similarity=0.5, prune_threshold=0.01)
        == "gated_cosine+below_prune"
    )


def test_classify_pair_missing_embedding_skips_gate():
    """recall() skips the cosine gate when either embedding is missing — the
    pair is classified by the prune cliff alone."""
    assert classify_pair(None, 0.02, min_similarity=0.5, prune_threshold=0.01) == "forms"
    assert classify_pair(None, 0.005, min_similarity=0.5, prune_threshold=0.01) == "below_prune"


def test_summarize_gate_audit_counts_and_probe_pairs():
    pairs = [
        PairAudit("q1", "a", "b", 0.6, 0.02, "forms", is_probe_gold_pair=False),
        PairAudit("q1", "a", "c", 0.3, 0.02, "gated_cosine", is_probe_gold_pair=False),
        PairAudit("q2", "a", "b", 0.6, 0.005, "below_prune", is_probe_gold_pair=False),
        PairAudit("q3", "s", "t", 0.4, 0.03, "gated_cosine", is_probe_gold_pair=True),
    ]
    summary = summarize_gate_audit(pairs)

    assert summary["pair_observations"] == 4
    assert summary["verdicts"] == {"forms": 1, "gated_cosine": 2, "below_prune": 1}
    # Probe gold pairs are the compounding-critical subset: every one that is
    # gated explains recovery lift staying at zero.
    assert summary["probe_gold_pairs"] == [
        {
            "query_id": "q3",
            "pair": ["s", "t"],
            "cosine": 0.4,
            "delta_w": 0.03,
            "verdict": "gated_cosine",
        }
    ]


def test_summarize_gate_audit_reports_noise_side_metrics():
    """#982 / Gate1: lowering the gate must not blow up non-gold (noise) edges.

    The audit reports the precision side alongside the recall side: how many
    formed edges are gold vs noise, and what fraction of non-gold pairs formed.
    """
    pairs = [
        PairAudit("q1", "a", "b", 0.6, 0.02, "forms", is_probe_gold_pair=True),
        PairAudit("q1", "a", "c", 0.6, 0.02, "forms", is_probe_gold_pair=False),
        PairAudit("q2", "a", "d", 0.3, 0.02, "gated_cosine", is_probe_gold_pair=False),
        PairAudit("q3", "s", "t", 0.4, 0.03, "gated_cosine", is_probe_gold_pair=True),
    ]
    summary = summarize_gate_audit(pairs)

    assert summary["formed_total"] == 2
    assert summary["formed_gold"] == 1
    assert summary["formed_non_gold"] == 1
    # precision of the formed-edge set w.r.t. gold pairs
    assert summary["edge_precision"] == pytest.approx(0.5)
    # non-gold pairs observed = a-c (formed) + a-d (gated) = 2; one formed
    assert summary["non_gold_pair_count"] == 2
    assert summary["non_gold_form_rate"] == pytest.approx(0.5)


def test_summarize_gate_audit_edge_precision_none_when_nothing_forms():
    """No formed edge → edge_precision is None (not a divide-by-zero / 0.0 lie)."""
    pairs = [
        PairAudit("q", "a", "b", 0.3, 0.02, "gated_cosine", is_probe_gold_pair=False),
        PairAudit("q", "s", "t", 0.3, 0.02, "gated_cosine", is_probe_gold_pair=True),
    ]
    summary = summarize_gate_audit(pairs)

    assert summary["formed_total"] == 0
    assert summary["edge_precision"] is None
    # one non-gold pair observed, none formed → 0.0 (defined: 0/1)
    assert summary["non_gold_pair_count"] == 1
    assert summary["non_gold_form_rate"] == pytest.approx(0.0)
