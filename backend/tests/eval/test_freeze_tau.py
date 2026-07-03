"""Deterministic tests for the Day-3 freeze-τ pure layer.

The live τ measurement (ingest + vector fetch on the rig) is driven manually
via ``python -m tests.eval.freeze_tau`` at corpus-freeze time; only the pure
pair-enumeration logic is unit-tested here.
"""

from __future__ import annotations

from tests.eval.freeze_tau import heldout_cross_topic_gold_pairs
from tests.eval.tools.corpus import Corpus, Document, Query


def _corpus(queries: list[Query]) -> Corpus:
    docs = (
        Document(id="m1", source="memory", text="m1 text"),
        Document(id="m2", source="memory", text="m2 text"),
        Document(id="r1", source="resource", text="r1 text"),
        Document(id="r2", source="resource", text="r2 text"),
    )
    return Corpus(meta={}, documents=docs, queries=tuple(queries))


def _q(qid: str, relevant: tuple[str, ...], split: str | None) -> Query:
    return Query(id=qid, bucket="cross-source", text=qid, relevant=relevant, split=split)


def test_pairs_come_only_from_heldout_multigold_and_cross_source():
    corpus = _corpus(
        [
            _q("q1", ("m1", "r1"), "heldout"),  # cross-topic pair -> included
            _q("q2", ("m1", "m2"), "heldout"),  # same-source pair -> excluded
            _q("q3", ("m2", "r2"), "public"),  # public -> excluded entirely
            _q("q4", ("r2",), "heldout"),  # single-gold -> not a probe
            _q("q5", ("m2", "r1", "r2"), "heldout"),  # mixed: m2-r1, m2-r2 in; r1-r2 out
        ]
    )
    pairs = heldout_cross_topic_gold_pairs(corpus)
    assert pairs == {
        frozenset(("m1", "r1")),
        frozenset(("m2", "r1")),
        frozenset(("m2", "r2")),
    }


def test_no_heldout_probes_yields_empty_set():
    corpus = _corpus([_q("q1", ("m1", "r1"), "public"), _q("q2", ("m1",), "heldout")])
    assert heldout_cross_topic_gold_pairs(corpus) == set()
