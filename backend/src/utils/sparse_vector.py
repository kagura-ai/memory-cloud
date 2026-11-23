"""Sparse vector builder for Qdrant native BM25 search.

Issue #16: Converts Sudachi-tokenized text into sparse vectors
using MurmurHash3 for deterministic token-to-index mapping.

Used with Qdrant's SparseVectorParams(modifier=Modifier.IDF),
which applies IDF weighting at query time (dot-product with IDF).
Modifier.IDF performs neither the term-frequency saturation nor the length
normalisation of Okapi BM25, so the document-side builders do (#2984): each
field's value of a term is

    w_f * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl_f / avgdl_f))

with ``w_f`` the field weight, ``tf`` the term's count in the field, ``dl_f``
the field's token count and ``avgdl_f`` the average token count of that field
over the live rows of the memory's context (``services.keyword_stats``). The
query side stays binary, so a query term scores ``idf * value``: Okapi BM25
with field weights.
"""

from collections import Counter
from dataclasses import dataclass

import mmh3

from utils.tokenizer import tokenize_for_search

#: Okapi BM25 term-frequency saturation (#2984). Module constants, not settings:
#: a change needs every point's sparse vector rebuilt
#: (``python -m src.cli.rebuild_keyword_vectors``).
BM25_K1 = 1.2
#: Okapi BM25 length normalisation strength (0 = none, 1 = full).
BM25_B = 0.75

#: Field weights of a memory point. ``lance_store._FIELD_REPEATS`` mirrors the
#: first three as token repetition.
SUMMARY_WEIGHT = 2.0
CONTEXT_SUMMARY_WEIGHT = 2.0
CONTENT_WEIGHT = 1.0
SUMMARY_READING_WEIGHT = 0.5
#: A resource point's single fulltext field, at the memory ``content`` weight.
RESOURCE_FULLTEXT_WEIGHT = 1.0


@dataclass(frozen=True)
class MemoryFieldStats:
    """Average token count per memory field over a context's live rows (#2984).

    ``None`` = no row of the context has that field indexed yet: the field is
    then weighted with its own length as the average (saturation only).
    """

    summary: float | None = None
    context_summary: float | None = None
    content: float | None = None
    summary_reading: float | None = None


def token_count(tokens_str: str) -> int:
    """The length ``dl`` of a field: its number of space-separated tokens."""
    return len(tokens_str.split()) if tokens_str else 0


def bm25_field_vector(tokens_str: str, weight: float, avgdl: float | None) -> dict[int, float]:
    """``{hash_index: value}`` of one field, BM25-weighted (#2984).

    Args:
        tokens_str: The field's space-separated tokens.
        weight: The field weight ``w_f``.
        avgdl: The field's average token count in the context. ``None`` or
            not positive (no statistics yet) = the field's own length, so
            only the saturation applies.

    Returns:
        The field's values; empty for an empty field. Tokens whose hashes
        collide add up, as their raw counts did before.
    """
    tokens = tokens_str.split() if tokens_str else []
    if not tokens:
        return {}
    dl = len(tokens)
    average = avgdl if avgdl is not None and avgdl > 0 else float(dl)
    norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / average)
    out: dict[int, float] = {}
    for token, tf in Counter(tokens).items():
        idx = hash_token(token)
        out[idx] = out.get(idx, 0.0) + weight * tf * (BM25_K1 + 1) / (tf + norm)
    return out


def hash_token(token: str) -> int:
    """Token-to-sparse-index hash used by the BM25 producer + drift consumer.

    Centralized so the document writer (sparse vector builder) and the
    drift-log reverse-lookup (reveal-terms endpoint) cannot diverge on
    `signed=False` or hash seed.
    """
    return mmh3.hash(token, signed=False)


def tokens_to_sparse_vector(
    tokens_str: str,
    weight: float = 1.0,
) -> dict[int, float]:
    """Convert space-separated tokens to {hash_index: weighted_tf} dict.

    Args:
        tokens_str: Space-separated lemma tokens (from tokenize_for_search)
        weight: Multiplier for TF values (field weighting)

    Returns:
        Dict mapping MurmurHash3 indices to weighted term frequencies
    """
    if not tokens_str:
        return {}
    tokens = tokens_str.split()
    counts = Counter(tokens)
    return {hash_token(token): count * weight for token, count in counts.items()}


def build_document_sparse_vector(
    summary_tokens: str,
    context_summary_tokens: str,
    content_tokens: str,
    summary_reading: str = "",
    *,
    stats: MemoryFieldStats | None = None,
) -> tuple[list[int], list[float]]:
    """Build combined sparse vector from all document fields.

    Each field is BM25-weighted (``bm25_field_vector``, #2984) with its own
    average from ``stats``, then the fields are summed per index. Field
    weights:
    - summary/context_summary: 2.0 (primary match)
    - content: 1.0 (body match)
    - summary_reading: 0.5 (hiragana query fallback, Issue #73)

    Args:
        summary_tokens: Sudachi-tokenized summary
        context_summary_tokens: Sudachi-tokenized context_summary
        content_tokens: Sudachi-tokenized content
        summary_reading: Katakana reading of summary (for hiragana queries)
        stats: The context's per-field averages; ``None`` = none known yet.

    Returns:
        (indices, values) tuple for SparseVector constructor
    """
    averages = stats if stats is not None else MemoryFieldStats()
    merged: dict[int, float] = {}
    for tokens, weight, avgdl in (
        (summary_tokens, SUMMARY_WEIGHT, averages.summary),
        (context_summary_tokens, CONTEXT_SUMMARY_WEIGHT, averages.context_summary),
        (content_tokens, CONTENT_WEIGHT, averages.content),
        (summary_reading, SUMMARY_READING_WEIGHT, averages.summary_reading),
    ):
        for idx, val in bm25_field_vector(tokens, weight, avgdl).items():
            merged[idx] = merged.get(idx, 0.0) + val

    if not merged:
        return [], []

    # Qdrant SparseVector accepts indices in any order — no sort needed
    indices = list(merged.keys())
    values = list(merged.values())
    return indices, values


def build_resource_sparse_vector(
    fulltext_content: str,
    avgdl: float | None = None,
    *,
    tokens: str | None = None,
) -> tuple[list[int], list[float]]:
    """Build sparse BM25 vector for a resource_indexer document (Issue #335).

    Resource payloads are flat — the indexer joins projected fields into a
    single `fulltext_content` string and has no summary/body/reading
    structure to weight separately (unlike Memory, see
    build_document_sparse_vector). We tokenize with Sudachi and BM25-weight
    the one field (#2984) at weight=1.0, intentionally matching the
    memory-side `content` field weight so corpus-wide IDF stays consistent
    across both write paths. Its average comes from the context's resource
    rows, apart from the memory fields: documents are not truncated and are
    usually much longer than a memory's indexed content. The helper lives in
    utils/sparse_vector (not the indexer) to keep memory_service and
    resource_indexer sharing a single source of truth for doc-side sparse
    encoding.

    Args:
        fulltext_content: Projected fulltext string from ResourceIndexer
        avgdl: The context's average fulltext token count; ``None`` = none
            known yet (saturation only).
        tokens: ``tokenize_for_search(fulltext_content)`` when the caller
            already has it (it also stores the token count); tokenized here
            otherwise.

    Returns:
        (indices, values) tuple for SparseVector constructor.
        Returns ([], []) for empty or tokenization-free input — callers
        should skip attaching a bm25 vector in that case.
    """
    field = tokens if tokens is not None else tokenize_for_search(fulltext_content)
    merged = bm25_field_vector(field, RESOURCE_FULLTEXT_WEIGHT, avgdl)
    if not merged:
        return [], []
    return list(merged.keys()), list(merged.values())


def build_query_sparse_vector(query_tokens: str) -> tuple[list[int], list[float]]:
    """Build sparse vector for a search query.

    Uses binary values (1.0) for each unique query token.
    Qdrant's IDF modifier applies the IDF at query time; the saturation and
    length normalisation are already in the document values (#2984).

    Args:
        query_tokens: Sudachi-tokenized query (space-separated)

    Returns:
        (indices, values) tuple for SparseVector constructor
    """
    if not query_tokens:
        return [], []
    unique_tokens = sorted(set(query_tokens.split()))
    indices = [hash_token(t) for t in unique_tokens]
    values = [1.0] * len(indices)
    return indices, values
