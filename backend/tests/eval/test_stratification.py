"""Stratification sanity gate (Issue #344) — deterministic, runs in CI.

Confirms the eval set spans a range of difficulty rather than clustering in one
regime (an all-easy corpus can't detect ranking regressions). This is a coverage
check on the corpus, not a quality gate on the system.
"""

from __future__ import annotations

from tests.eval.tools.stratify import difficulty_distribution, stratify_corpus


def test_every_query_is_stratified():
    strata = stratify_corpus()
    assert len(strata) >= 30
    for s in strata:
        assert s.bm25_rank_label in {"easy", "medium", "hard"}
        assert s.specificity >= 0.0
        assert 0.0 <= s.corpus_overlap <= 1.0


def test_corpus_spans_multiple_difficulty_regimes():
    """At least two of the three difficulty regimes must be populated.

    A golden set that is entirely 'easy' (every relevant doc ranks #1 under
    BM25-only) cannot surface ranking regressions — the hiragana-only and
    semantic buckets exist precisely to push lexical BM25 into 'hard'.
    """
    dist = difficulty_distribution(stratify_corpus())
    populated = [label for label, n in dist.items() if n > 0]
    assert len(populated) >= 2, f"difficulty too concentrated: {dist}"
    # The hard regime specifically must exist (hiragana-only is BM25-hard).
    assert dist["hard"] > 0, f"no hard queries — lexical difficulty not covered: {dist}"
