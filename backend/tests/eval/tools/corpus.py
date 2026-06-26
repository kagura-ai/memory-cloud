"""Golden corpus loader + tokenization helpers (Issue #344).

Loads ``fixtures/golden_corpus.yaml`` into typed dataclasses and provides the
shared BM25-aligned tokenization used by both the leakage check and the
stratifier. Tokenization reuses the production ``tokenize_for_search`` so the
offline analysis sees the same tokens the live BM25 index would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Source kinds a corpus document may carry (aligned with the live ``source``
# dimension used by source_recall_share).
SOURCE_MEMORY = "memory"
SOURCE_RESOURCE = "resource"
_VALID_SOURCES = {SOURCE_MEMORY, SOURCE_RESOURCE}

# The six difficulty/coverage buckets every query must belong to.
BUCKETS = (
    "retrieval-exact",
    "retrieval-semantic",
    "hiragana-only",
    "cross-source",
    "resource-only",
    "memory-only",
)

# The 3-gram leakage rule is too strict for the exact-match bucket — those
# queries are *meant* to share a verbatim phrase with the target doc, so the
# 3-gram check would flag every well-formed exact query (CAIO gate1 note).
THREE_GRAM_EXEMPT_BUCKETS = frozenset({"retrieval-exact"})

_CORPUS_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "golden_corpus.yaml"


@dataclass(frozen=True)
class Document:
    id: str
    source: str
    text: str


@dataclass(frozen=True)
class Query:
    id: str
    bucket: str
    text: str
    relevant: tuple[str, ...]
    # Optional reinforce-eval stratum (Issue #1069): "current_fact" (the canonical
    # answer has been adopted/confirmed — reinforce should help) vs "rare" (gold is
    # a zero-adoption memory — reinforce must not bury it). None for the static
    # golden corpus, which is not stratified by adoption.
    population: str | None = None


@dataclass(frozen=True)
class Corpus:
    meta: dict[str, Any]
    documents: tuple[Document, ...]
    queries: tuple[Query, ...]

    @property
    def docs_by_id(self) -> dict[str, Document]:
        return {d.id: d for d in self.documents}

    def queries_in_bucket(self, bucket: str) -> list[Query]:
        return [q for q in self.queries if q.bucket == bucket]


def load_corpus(path: Path | None = None) -> Corpus:
    """Parse the golden corpus YAML into a :class:`Corpus`.

    Performs a minimal source-kind check (every document ``source`` must be a
    known kind) so a typo'd source fails fast at load. Deeper structural
    validation (bucket membership, label references, counts) lives in
    ``test_corpus_schema.py``.
    """
    p = path or _CORPUS_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    documents = tuple(
        Document(id=d["id"], source=d["source"], text=d["text"]) for d in raw.get("documents", [])
    )
    bad_sources = sorted({d.source for d in documents if d.source not in _VALID_SOURCES})
    if bad_sources:
        raise ValueError(
            f"golden corpus has document(s) with unknown source(s) {bad_sources}; "
            f"valid sources are {sorted(_VALID_SOURCES)}"
        )
    queries = tuple(
        Query(
            id=q["id"],
            bucket=q["bucket"],
            text=q["text"],
            relevant=tuple(q.get("relevant", [])),
            population=q.get("population"),
        )
        for q in raw.get("queries", [])
    )
    return Corpus(meta=raw.get("meta", {}), documents=documents, queries=queries)


@lru_cache(maxsize=4096)
def tokens(text: str) -> tuple[str, ...]:
    """BM25-aligned token tuple for ``text`` (reuses production tokenizer).

    Falls back gracefully when Sudachi is unavailable (the production tokenizer
    lowercases non-CJK / returns lowercase on error), so the deterministic CI
    gate does not hard-depend on the Sudachi dictionary being installed.
    """
    # Imported lazily so importing this module (e.g. for the loader alone) does
    # not pull the tokenizer + its optional Sudachi backend.
    from utils.tokenizer import tokenize_for_search

    joined = tokenize_for_search(text) or ""
    return tuple(t for t in joined.split() if t)


def ngrams(seq: tuple[str, ...], n: int) -> set[tuple[str, ...]]:
    """Set of contiguous ``n``-gram tuples over a token sequence."""
    if len(seq) < n:
        return set()
    return {tuple(seq[i : i + n]) for i in range(len(seq) - n + 1)}


@dataclass(frozen=True)
class TokenStats:
    """Corpus-level token statistics for IDF-based analysis."""

    doc_count: int
    df: dict[str, int] = field(default_factory=dict)  # document frequency
    idf: dict[str, float] = field(default_factory=dict)

    def idf_percentile(self, pct: float) -> float:
        """The ``pct`` (0-100) percentile of IDF values across the vocabulary."""
        if not self.idf:
            return 0.0
        ordered = sorted(self.idf.values())
        # Nearest-rank percentile — deterministic, no interpolation.
        rank = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
        return ordered[rank]


def compute_token_stats(documents: tuple[Document, ...]) -> TokenStats:
    """Document frequency + smoothed IDF over the corpus documents.

    IDF uses the standard ``log((N + 1) / (df + 1)) + 1`` smoothing so a term in
    every doc still gets a small positive weight (and division-by-zero is
    impossible). Deterministic given the corpus + tokenizer.
    """
    import math

    n = len(documents)
    df: dict[str, int] = {}
    for doc in documents:
        for tok in set(tokens(doc.text)):
            df[tok] = df.get(tok, 0) + 1
    idf = {tok: math.log((n + 1) / (count + 1)) + 1.0 for tok, count in df.items()}
    return TokenStats(doc_count=n, df=df, idf=idf)
