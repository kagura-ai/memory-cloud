"""Leakage gate for the golden corpus (Issue #344) — fail-loud, runs in CI.

Pure token analysis (no Qdrant / embeddings), so this is a normal-CI gate: if
any (query, relevant_doc) pair leaks under the three rules in
``tools/leakage_check.py``, this test fails and prints every flag. A leaking
corpus inflates retrieval metrics and hides regressions, so the corpus must stay
leakage-free.
"""

from __future__ import annotations

from tests.eval.tools.corpus import Corpus, Document, Query, compute_token_stats
from tests.eval.tools.leakage_check import (
    MIN_DOCS_FOR_RARE_TERM,
    check_query,
    run_leakage_check,
)


def test_golden_corpus_has_no_leakage():
    flags = run_leakage_check()
    assert not flags, "golden corpus leakage detected:\n" + "\n".join(f"  {f}" for f in flags)


def _synthetic_corpus(n_docs: int, *, planted_unique: str) -> Corpus:
    """A corpus of ``n_docs`` docs sharing filler vocabulary, with one doc
    carrying a planted unique term — and a query that uses that term."""
    docs = [
        Document(id=f"d{i}", source="memory", text=f"common filler text alpha beta gamma item {i}")
        for i in range(n_docs)
    ]
    # Doc d0 alone carries the planted unique term.
    docs[0] = Document(id="d0", source="memory", text=f"common filler {planted_unique} alpha beta")
    queries = (
        Query(
            id="q0", bucket="memory-only", text=f"find the {planted_unique} note", relevant=("d0",)
        ),
    )
    return Corpus(meta={}, documents=tuple(docs), queries=queries)


def test_rare_term_rule_fires_at_scale():
    """At >= MIN_DOCS_FOR_RARE_TERM docs, a planted unique-term leak IS flagged
    (the rule is genuinely active, not silently dead)."""
    corpus = _synthetic_corpus(MIN_DOCS_FOR_RARE_TERM, planted_unique="zzphylactery")
    stats = compute_token_stats(corpus.documents)
    flags = check_query(corpus.queries[0], corpus, stats.df, stats.idf, apply_rare_term=True)
    rare = [f for f in flags if f.rule == "rare_term"]
    assert rare, "rare-term rule should flag the planted unique-term leak at scale"
    assert any("zzphylactery" in f.detail for f in rare)


def test_rare_term_rule_gated_off_below_scale():
    """Below the document threshold the rule does not fire (hapax is the norm)."""
    corpus = _synthetic_corpus(MIN_DOCS_FOR_RARE_TERM, planted_unique="zzphylactery")
    stats = compute_token_stats(corpus.documents)
    flags = check_query(corpus.queries[0], corpus, stats.df, stats.idf, apply_rare_term=False)
    assert not [f for f in flags if f.rule == "rare_term"]
