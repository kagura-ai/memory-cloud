"""Structural validation of the golden corpus (Issue #344) — runs in CI.

Pins the corpus contract so a hand-edit that breaks the harness fails as a test,
not as an obscure runtime error during the live measurement:
- >= 30 queries, all six buckets represented;
- every query has >= 1 relevant doc, and every label references an existing doc;
- document ids and query ids are unique; every source is a known kind.

Note on label count: the #335 guidance suggested 3-5 labels per query, but that
assumed a larger corpus. This frozen starter set uses binary relevance with >= 1
relevant doc per query (the harness only requires a non-empty gold set); see
README.md.
"""

from __future__ import annotations

from tests.eval.tools.corpus import _VALID_SOURCES, BUCKETS, load_corpus

MIN_QUERIES = 30


def test_corpus_loads_and_meets_minimum_size():
    corpus = load_corpus()
    assert len(corpus.queries) >= MIN_QUERIES, (
        f"expected >= {MIN_QUERIES} queries, got {len(corpus.queries)}"
    )
    assert len(corpus.documents) > 0


def test_all_buckets_represented():
    corpus = load_corpus()
    present = {q.bucket for q in corpus.queries}
    missing = set(BUCKETS) - present
    assert not missing, f"buckets with no queries: {sorted(missing)}"
    unknown = present - set(BUCKETS)
    assert not unknown, f"queries in unknown buckets: {sorted(unknown)}"


def test_document_ids_unique_and_sources_valid():
    corpus = load_corpus()
    ids = [d.id for d in corpus.documents]
    assert len(ids) == len(set(ids)), "duplicate document ids"
    bad = {d.id: d.source for d in corpus.documents if d.source not in _VALID_SOURCES}
    assert not bad, f"documents with invalid source: {bad}"


def test_query_ids_unique():
    corpus = load_corpus()
    ids = [q.id for q in corpus.queries]
    assert len(ids) == len(set(ids)), "duplicate query ids"


def test_every_query_has_resolvable_labels():
    corpus = load_corpus()
    doc_ids = set(corpus.docs_by_id)
    for q in corpus.queries:
        assert q.relevant, f"query {q.id} has no relevant docs"
        dangling = [d for d in q.relevant if d not in doc_ids]
        assert not dangling, f"query {q.id} references missing docs: {dangling}"


def test_cross_source_queries_actually_span_sources():
    """A cross-source query's gold set must include both a memory and a resource doc."""
    corpus = load_corpus()
    docs = corpus.docs_by_id
    for q in corpus.queries_in_bucket("cross-source"):
        sources = {docs[d].source for d in q.relevant if d in docs}
        assert len(sources) >= 2, (
            f"cross-source query {q.id} only covers sources {sources}; expected >= 2"
        )


def test_source_scoped_buckets_are_consistent():
    """resource-only / memory-only gold sets must not mix in the other source."""
    corpus = load_corpus()
    docs = corpus.docs_by_id
    for bucket, expected in (("resource-only", "resource"), ("memory-only", "memory")):
        for q in corpus.queries_in_bucket(bucket):
            sources = {docs[d].source for d in q.relevant if d in docs}
            assert sources == {expected}, (
                f"{bucket} query {q.id} has sources {sources}; expected {{{expected!r}}}"
            )
