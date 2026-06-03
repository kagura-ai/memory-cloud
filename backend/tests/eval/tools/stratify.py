"""Auto-stratification of golden queries by difficulty (Issue #344).

Assigns each query a deterministic difficulty signal from three indicators
(synthesis of the #335 panel: CAIO + Stats + DS + CS):

- **spec(q)** — average corpus IDF of the query's terms. High = specific/rare
  vocabulary (usually easier to retrieve); low = generic terms (harder).
- **bm25_rank** — rank of the query's first relevant doc under a BM25-only
  ranking over the corpus → ``easy`` (rank 1), ``medium`` (rank 2-3), ``hard``
  (rank > 3 or unranked). This is the lexical-difficulty pseudo-label.
- **corpus_overlap** — overlap coefficient = fraction of the query's tokens that
  are among the corpus's top-1000 most-frequent tokens (|q ∩ top| / |q|). High =
  query rides on common vocabulary (a weak-signal query). (This is intentionally
  the overlap coefficient, not true Jaccard: |q ∪ top| ≈ 1000 would make a true
  Jaccard vanishingly small and uninformative.)

These are *descriptive* — they characterize the eval set's coverage, they are
NOT pass/fail gates. Used to confirm the corpus spans easy→hard rather than
clustering in one regime. Everything here is pure-Python and deterministic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from tests.eval.tools.corpus import (
    Corpus,
    Query,
    TokenStats,
    compute_token_stats,
    load_corpus,
    tokens,
)

# Okapi BM25 parameters (standard defaults; the corpus is small so tuning is moot).
_BM25_K1 = 1.5
_BM25_B = 0.75

# bm25_rank label thresholds (1-based rank of the first relevant doc).
_EASY_MAX_RANK = 1
_MEDIUM_MAX_RANK = 3

_TOP_CORPUS_TOKENS = 1000


@dataclass(frozen=True)
class QueryStrata:
    query_id: str
    bucket: str
    specificity: float
    bm25_rank_label: str  # "easy" | "medium" | "hard"
    first_relevant_rank: int | None  # 1-based, None if no relevant doc ranked
    corpus_overlap: float


def _bm25_scores(
    query_tokens: tuple[str, ...],
    corpus: Corpus,
    stats: TokenStats,
) -> dict[str, float]:
    """Okapi BM25 score of each document for the query (deterministic)."""
    doc_tokens = {d.id: tokens(d.text) for d in corpus.documents}
    lengths = {doc_id: len(toks) for doc_id, toks in doc_tokens.items()}
    avgdl = (sum(lengths.values()) / len(lengths)) if lengths else 0.0
    q_terms = set(query_tokens)

    scores: dict[str, float] = {}
    for doc_id, toks in doc_tokens.items():
        tf = Counter(toks)
        dl = lengths[doc_id]
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            # BM25 uses an IDF variant; reuse the corpus smoothed IDF for a
            # deterministic, dependency-free score (ordering is what matters).
            idf = stats.idf.get(term, 0.0)
            freq = tf[term]
            denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0))
            score += idf * (freq * (_BM25_K1 + 1)) / denom if denom else 0.0
        scores[doc_id] = score
    return scores


def _first_relevant_rank(scores: dict[str, float], relevant: tuple[str, ...]) -> int | None:
    """1-based rank of the highest-scoring relevant doc (None if none ranked)."""
    if not relevant:
        return None
    # Deterministic ordering: score desc, then doc_id asc to break ties.
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    rel = set(relevant)
    for rank, (doc_id, score) in enumerate(ordered, start=1):
        if doc_id in rel and score > 0.0:
            return rank
    return None


def _rank_label(rank: int | None) -> str:
    if rank is not None and rank <= _EASY_MAX_RANK:
        return "easy"
    if rank is not None and rank <= _MEDIUM_MAX_RANK:
        return "medium"
    return "hard"


def _top_corpus_tokens(corpus: Corpus, n: int = _TOP_CORPUS_TOKENS) -> set[str]:
    counter: Counter[str] = Counter()
    for doc in corpus.documents:
        counter.update(tokens(doc.text))
    return {tok for tok, _ in counter.most_common(n)}


def stratify_query(
    query: Query,
    corpus: Corpus,
    stats: TokenStats,
    top_tokens: set[str],
) -> QueryStrata:
    q_tokens = tokens(query.text)
    q_set = set(q_tokens)
    spec = sum(stats.idf.get(t, 0.0) for t in q_set) / len(q_set) if q_set else 0.0
    rank = _first_relevant_rank(_bm25_scores(q_tokens, corpus, stats), query.relevant)
    overlap = (len(q_set & top_tokens) / len(q_set)) if q_set else 0.0
    return QueryStrata(
        query_id=query.id,
        bucket=query.bucket,
        specificity=round(spec, 4),
        bm25_rank_label=_rank_label(rank),
        first_relevant_rank=rank,
        corpus_overlap=round(overlap, 4),
    )


def stratify_corpus(corpus: Corpus | None = None) -> list[QueryStrata]:
    c = corpus or load_corpus()
    stats = compute_token_stats(c.documents)
    top_tokens = _top_corpus_tokens(c)
    return [stratify_query(q, c, stats, top_tokens) for q in c.queries]


def difficulty_distribution(strata: list[QueryStrata]) -> dict[str, int]:
    """Count of queries per bm25_rank difficulty label."""
    dist: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for s in strata:
        dist[s.bm25_rank_label] = dist.get(s.bm25_rank_label, 0) + 1
    return dist


def main() -> int:
    strata = stratify_corpus()
    dist = difficulty_distribution(strata)
    print(f"stratify: {len(strata)} queries — difficulty {dist}")
    for s in strata:
        print(
            f"  {s.query_id:24s} {s.bucket:18s} spec={s.specificity:6.3f} "
            f"rank={s.first_relevant_rank} → {s.bm25_rank_label:6s} "
            f"corpus_overlap={s.corpus_overlap:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
