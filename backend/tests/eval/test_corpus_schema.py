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

from pathlib import Path

import pytest

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


# --- split / graded schema extension (kagura_L prep, prereg-v1 H3) ---------


def _write_corpus(tmp_path: Path, queries_yaml: str) -> Path:
    """Write a minimal 2-doc corpus YAML with the given ``queries:`` body."""
    content = f"""
meta:
  version: 1
documents:
  - id: doc_a
    source: memory
    text: "alpha"
  - id: doc_b
    source: memory
    text: "beta"
queries:
{queries_yaml}
"""
    p = tmp_path / "corpus.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_golden_corpus_has_no_split_or_graded_by_default():
    """The frozen starter corpus is a legacy corpus: split/graded are unset."""
    corpus = load_corpus()
    for q in corpus.queries:
        assert q.split is None
        assert q.graded is None


def test_split_and_graded_round_trip(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: retrieval-exact
    text: "find alpha"
    relevant: [doc_a]
    split: heldout
    graded:
      doc_a: 3
      doc_b: 1
""",
    )
    corpus = load_corpus(p)
    q = corpus.queries[0]
    assert q.split == "heldout"
    assert q.graded == {"doc_a": 3, "doc_b": 1}


def test_bad_split_value_raises(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: retrieval-exact
    text: "find alpha"
    relevant: [doc_a]
    split: bogus
""",
    )
    with pytest.raises(ValueError, match="q1"):
        load_corpus(p)


def test_bad_grade_value_raises(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: retrieval-exact
    text: "find alpha"
    relevant: [doc_a]
    graded:
      doc_a: 5
""",
    )
    with pytest.raises(ValueError, match="q1"):
        load_corpus(p)


# --- update pair schema extension (Day-5, prereg-v1 H4) ---------------------


def test_golden_corpus_has_no_update_by_default():
    corpus = load_corpus()
    for q in corpus.queries:
        assert q.update is None


def test_update_round_trip(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: update
    text: "find alpha"
    relevant: [doc_b]
    update:
      current: doc_b
      stale: doc_a
""",
    )
    q = load_corpus(p).queries[0]
    assert q.update == {"current": "doc_b", "stale": "doc_a"}


def test_update_wrong_keys_raise(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: update
    text: "find alpha"
    relevant: [doc_b]
    update:
      current: doc_b
      old: doc_a
""",
    )
    with pytest.raises(ValueError, match="q1"):
        load_corpus(p)


def test_update_missing_key_raises(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: update
    text: "find alpha"
    relevant: [doc_b]
    update:
      current: doc_b
""",
    )
    with pytest.raises(ValueError, match="q1"):
        load_corpus(p)


def test_update_empty_value_raises(tmp_path: Path):
    p = _write_corpus(
        tmp_path,
        """\
  - id: q1
    bucket: update
    text: "find alpha"
    relevant: [doc_b]
    update:
      current: doc_b
      stale: ""
""",
    )
    with pytest.raises(ValueError, match="q1"):
        load_corpus(p)
