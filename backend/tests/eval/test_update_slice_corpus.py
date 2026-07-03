"""update_slice corpus gates (Day-5, prereg-v1 H4) — fail-loud regression tests.

The frozen update-slice fixture must keep passing every gate that justified its
freeze: shape/counts, update-pair reference validity, longitudinal ingest
ordering (v1 block -> fillers -> v2 block; ingest order = list order), zero
leakage flags, and a recomputable content hash.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from tests.eval.tools.corpus import load_corpus
from tests.eval.tools.leakage_check import run_leakage_check

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "update_slice.yaml"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(_FIXTURE)


def test_shape_and_counts(corpus):
    assert len(corpus.documents) == 130
    assert len(corpus.queries) == 50
    assert all(q.bucket == "update" for q in corpus.queries)
    assert all(q.split == "heldout" for q in corpus.queries)
    src = Counter(d.source for d in corpus.documents)
    assert set(src) == {"memory", "resource"}


def test_update_pairs_valid(corpus):
    docset = {d.id for d in corpus.documents}
    for q in corpus.queries:
        assert q.update is not None, q.id
        cur, stale = q.update["current"], q.update["stale"]
        assert cur in docset and stale in docset, q.id
        assert q.relevant == (cur,), q.id
        assert stale not in q.relevant, q.id
        # pair naming discipline: uNN-v2 current supersedes uNN-v1 stale
        assert cur.endswith("-v2") and stale.endswith("-v1") and cur[:-3] == stale[:-3], q.id


def test_longitudinal_ordering(corpus):
    """Ingest order = list order: all v1, then fillers, then all v2."""
    ids = [d.id for d in corpus.documents]
    assert ids[:50] == [f"u{n:02d}-v1" for n in range(1, 51)]
    assert all(i.startswith("f") for i in ids[50:80]), ids[50:80][:3]
    assert ids[80:] == [f"u{n:02d}-v2" for n in range(1, 51)]


def test_pair_sources_match(corpus):
    by_id = corpus.docs_by_id
    for q in corpus.queries:
        assert by_id[q.update["current"]].source == by_id[q.update["stale"]].source, q.id


def test_leakage_gate_zero_flags(corpus):
    flags = run_leakage_check(corpus)
    detail = [str(f) for f in flags]
    assert not flags, "leakage flags:\n" + "\n".join(detail)


def test_content_hash_recomputes(corpus):
    """meta.content_sha256 == sha256 of the canonical docs+queries JSON."""
    documents = [{"id": d.id, "source": d.source, "text": d.text} for d in corpus.documents]
    queries = [
        {
            "id": q.id,
            "bucket": q.bucket,
            "text": q.text,
            "relevant": list(q.relevant),
            "split": q.split,
            "update": q.update,
        }
        for q in corpus.queries
    ]
    canonical = json.dumps(
        {"documents": documents, "queries": queries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert corpus.meta.get("content_sha256") == sha
