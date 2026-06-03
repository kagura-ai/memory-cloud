"""Leakage check for the golden retrieval corpus (Issue #344).

A query "leaks" when it is so lexically close to its own relevant doc that any
retriever trivially wins — which inflates the metric and hides real regressions.
Three deterministic rules flag a (query, relevant_doc) pair:

1. **Token Jaccard > 0.5** — query and doc share more than half their token
   vocabulary. Applies to every bucket.
2. **Any shared 3-gram** — a verbatim 3-token run appears in both. Applies to
   every bucket EXCEPT ``retrieval-exact`` (those queries are *designed* to
   share a phrase with the target, so the rule would flag every valid one).
3. **Rare-term unique cooccurrence** — the query contains a corpus-unique term
   (document frequency == 1) that therefore appears ONLY in this relevant doc.
   That term alone makes retrieval a giveaway. A ``df == 1`` term necessarily
   carries the maximum IDF, so this is the precise, robust form of the #335
   "high-IDF term unique to the relevant doc" rule (an IDF-percentile gate with
   a strict ``>`` is silently dead on hapax-heavy corpora). **Scale-gated:** the
   rule fires only when the corpus has at least ``MIN_DOCS_FOR_RARE_TERM``
   documents. Below that size almost every content word is a hapax (``df == 1``
   is the norm, not a rarity signal), so applying it would flag normal on-topic
   overlap — the Jaccard and 3-gram rules cover the harmful cases at small N.
   The rule activates automatically as the corpus grows (the scale at which
   ``df == 1`` becomes a genuine giveaway, the regime #335 assumed).

Run as a module (``python -m tests.eval.tools.leakage_check``) for a CLI report,
or via ``test_leakage.py`` as a fail-loud pytest gate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from tests.eval.tools.corpus import (
    THREE_GRAM_EXEMPT_BUCKETS,
    Corpus,
    Query,
    compute_token_stats,
    load_corpus,
    ngrams,
    tokens,
)

# Rule constants (the #335 panel thresholds). Kept module-level so tests and the
# README reference one source of truth.
JACCARD_THRESHOLD = 0.5
NGRAM_N = 3
# Rule 3 uses document-frequency == 1 (corpus-unique) rather than an IDF
# percentile — see the module docstring for why the percentile gate is dead.
# It is scale-gated: below this many documents, df == 1 is the norm (hapax-heavy
# small corpus), so the rule would flag normal on-topic overlap. It activates
# once the corpus is large enough for df == 1 to be a genuine rarity signal.
MIN_DOCS_FOR_RARE_TERM = 50


@dataclass(frozen=True)
class LeakageFlag:
    query_id: str
    doc_id: str
    rule: str  # "jaccard" | "ngram" | "rare_term"
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.query_id} ↔ {self.doc_id}: {self.detail}"


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def check_query(
    query: Query,
    corpus: Corpus,
    df: dict[str, int],
    idf: dict[str, float],
    *,
    apply_rare_term: bool = True,
) -> list[LeakageFlag]:
    """Return every leakage flag for a single query against its relevant docs.

    Rule 3 keys off ``df`` (document frequency) directly: a query term with
    ``df == 1`` occurs in exactly one corpus document, so if that document is
    the relevant one, the term is a unique giveaway. This is the precise meaning
    of "rare-term unique cooccurrence" — and ``df == 1`` terms necessarily carry
    the maximum IDF, so an IDF-percentile gate is both redundant and (with a
    strict ``>`` against p90, which hapax-heavy corpora push to the IDF max)
    silently dead. Using ``df`` also makes the rule O(1) per term instead of an
    O(docs²) re-scan.
    """
    flags: list[LeakageFlag] = []
    q_tokens = tokens(query.text)
    q_set = set(q_tokens)
    q_grams = ngrams(q_tokens, NGRAM_N)
    docs_by_id = corpus.docs_by_id

    # Corpus-unique query terms (occur in exactly one document anywhere).
    # Empty when the rare-term rule is scale-gated off (small corpus).
    unique_q_terms = {t for t in q_set if df.get(t, 0) == 1} if apply_rare_term else set()

    for doc_id in query.relevant:
        doc = docs_by_id.get(doc_id)
        if doc is None:
            continue  # dangling label — the schema test reports this separately
        d_tokens = tokens(doc.text)
        d_set = set(d_tokens)

        # Rule 1 — token Jaccard.
        j = _jaccard(q_set, d_set)
        if j > JACCARD_THRESHOLD:
            flags.append(
                LeakageFlag(
                    query.id, doc_id, "jaccard", f"token Jaccard {j:.2f} > {JACCARD_THRESHOLD}"
                )
            )

        # Rule 2 — shared 3-gram (exact-bucket exempt).
        if query.bucket not in THREE_GRAM_EXEMPT_BUCKETS:
            shared = q_grams & ngrams(d_tokens, NGRAM_N)
            if shared:
                example = " ".join(next(iter(shared)))
                flags.append(
                    LeakageFlag(query.id, doc_id, "ngram", f"shared {NGRAM_N}-gram: '{example}'")
                )

        # Rule 3 — rare-term unique cooccurrence (df == 1 term in this doc).
        for term in unique_q_terms:
            if term in d_set:
                flags.append(
                    LeakageFlag(
                        query.id,
                        doc_id,
                        "rare_term",
                        f"corpus-unique term '{term}' (df=1, idf {idf.get(term, 0.0):.2f}) "
                        f"occurs only in this doc",
                    )
                )
    return flags


def run_leakage_check(corpus: Corpus | None = None) -> list[LeakageFlag]:
    """Run all three rules over the whole corpus and return the flagged pairs."""
    c = corpus or load_corpus()
    stats = compute_token_stats(c.documents)
    apply_rare_term = len(c.documents) >= MIN_DOCS_FOR_RARE_TERM
    flags: list[LeakageFlag] = []
    for q in c.queries:
        flags.extend(check_query(q, c, stats.df, stats.idf, apply_rare_term=apply_rare_term))
    return flags


def main() -> int:
    flags = run_leakage_check()
    if not flags:
        print("leakage_check: OK — no flagged (query, relevant_doc) pairs")
        return 0
    print(f"leakage_check: {len(flags)} flagged pair(s):", file=sys.stderr)
    for f in flags:
        print(f"  {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
