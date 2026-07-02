"""kagura_L corpus gates (Day-3, docs/02 §1.1-1.2) — fail-loud regression tests.

The frozen kagura_L fixture must keep passing every gate that justified its
freeze: schema/count shape, graded-label consistency, split discipline,
probe validity, ZERO leakage flags (all three rules live at 300 docs), and
stratification coverage (≥3 difficulty regimes, ≥20% hard — the docs/02 §1.2
item-4 proportion gate). golden_corpus.yaml keeps its own, untouched tests.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.eval.tools.corpus import load_corpus
from tests.eval.tools.leakage_check import run_leakage_check
from tests.eval.tools.stratify import stratify_corpus

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "kagura_l.yaml"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(_FIXTURE)


def test_shape_and_counts(corpus):
    assert len(corpus.documents) == 300
    src = Counter(d.source for d in corpus.documents)
    assert src == {"memory": 180, "resource": 120}
    assert len(corpus.queries) == 280
    split = Counter(q.split for q in corpus.queries)
    assert split == {"heldout": 240, "public": 40}
    buckets = Counter(q.bucket for q in corpus.queries)
    assert buckets == {
        "cross-source": 60,
        "retrieval-exact": 40,
        "retrieval-semantic": 50,
        "hiragana-only": 35,
        "resource-only": 45,
        "memory-only": 50,
    }
    assert corpus.meta.get("content_sha256"), "frozen corpus must carry its content hash"


def test_labels_and_references(corpus):
    docset = {d.id for d in corpus.documents}
    for q in corpus.queries:
        assert q.relevant, q.id
        assert set(q.relevant) <= docset, q.id
        assert q.graded is not None, q.id
        assert set(q.graded) <= docset, q.id
        # binary gold SoT == graded >= 2; primary gold is grade 3.
        assert set(q.relevant) == {d for d, g in q.graded.items() if g >= 2}, q.id
        assert q.graded[q.relevant[0]] == 3, q.id


def test_probe_population(corpus):
    """>= 50 valid held-out cross-source multi-gold probes (N_probe floor)."""
    by_id = corpus.docs_by_id
    valid = [
        q
        for q in corpus.queries
        if q.bucket == "cross-source"
        and q.split == "heldout"
        and len(q.relevant) >= 2
        and {by_id[d].source for d in q.relevant} == {"memory", "resource"}
    ]
    assert len(valid) >= 50, f"only {len(valid)} valid probes"


def test_split_discipline(corpus):
    for q in corpus.queries:
        assert q.split in ("public", "heldout"), q.id
        if q.bucket in ("cross-source", "hiragana-only"):
            assert q.split == "heldout", q.id


def test_leakage_gate_zero_flags(corpus):
    """All three leakage rules live (300 docs >= MIN_DOCS_FOR_RARE_TERM) — zero flags."""
    flags = run_leakage_check(corpus)
    detail = [str(f) for f in flags]
    assert not flags, "leakage flags:\n" + "\n".join(detail)


def test_stratification_spans_regimes_with_hard_proportion(corpus):
    """docs/02 §1.2 item 4: all three bm25 difficulty labels, >= 20% hard."""
    strata = stratify_corpus(corpus)
    dist = Counter(s.bm25_rank_label for s in strata)
    assert set(dist) == {"easy", "medium", "hard"}, dist
    hard_share = dist["hard"] / len(strata)
    assert hard_share >= 0.20, f"hard share {hard_share:.2%} < 20%: {dist}"
