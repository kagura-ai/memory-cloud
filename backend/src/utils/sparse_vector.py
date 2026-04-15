"""Sparse vector builder for Qdrant native BM25 search.

Issue #16: Converts Sudachi-tokenized text into sparse vectors
using MurmurHash3 for deterministic token-to-index mapping.

Used with Qdrant's SparseVectorParams(modifier=Modifier.IDF),
which applies IDF weighting at query time (dot-product with IDF).
Length normalization and the k1/b saturation of textbook Okapi BM25
are NOT performed by Modifier.IDF — pre-weight tf values in the
document-side builders to shape ranking.
"""

from collections import Counter

import mmh3

from utils.tokenizer import tokenize_for_search


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
    return {mmh3.hash(token, signed=False): count * weight for token, count in counts.items()}


def build_document_sparse_vector(
    summary_tokens: str,
    context_summary_tokens: str,
    content_tokens: str,
    summary_reading: str = "",
) -> tuple[list[int], list[float]]:
    """Build combined sparse vector from all document fields.

    Field weights:
    - summary/context_summary: 2.0 (primary match)
    - content: 1.0 (body match)
    - summary_reading: 0.5 (hiragana query fallback, Issue #73)

    Args:
        summary_tokens: Sudachi-tokenized summary
        context_summary_tokens: Sudachi-tokenized context_summary
        content_tokens: Sudachi-tokenized content
        summary_reading: Katakana reading of summary (for hiragana queries)

    Returns:
        (indices, values) tuple for SparseVector constructor
    """
    merged: dict[int, float] = {}
    for idx, val in tokens_to_sparse_vector(summary_tokens, weight=2.0).items():
        merged[idx] = merged.get(idx, 0.0) + val
    for idx, val in tokens_to_sparse_vector(context_summary_tokens, weight=2.0).items():
        merged[idx] = merged.get(idx, 0.0) + val
    for idx, val in tokens_to_sparse_vector(content_tokens, weight=1.0).items():
        merged[idx] = merged.get(idx, 0.0) + val
    if summary_reading:
        for idx, val in tokens_to_sparse_vector(summary_reading, weight=0.5).items():
            merged[idx] = merged.get(idx, 0.0) + val

    if not merged:
        return [], []

    # Qdrant SparseVector accepts indices in any order — no sort needed
    indices = list(merged.keys())
    values = list(merged.values())
    return indices, values


def build_resource_sparse_vector(fulltext_content: str) -> tuple[list[int], list[float]]:
    """Build sparse BM25 vector for a resource_indexer document (Issue #335).

    Resource payloads are flat — the indexer joins projected fields into a
    single `fulltext_content` string and has no summary/body/reading
    structure to weight separately (unlike Memory, see
    build_document_sparse_vector). We tokenize with Sudachi and emit the
    raw term frequencies at weight=1.0, intentionally matching the
    memory-side `content` field weight so corpus-wide IDF stays consistent
    across both write paths. The helper lives in utils/sparse_vector (not
    the indexer) to keep memory_service and resource_indexer sharing a
    single source of truth for doc-side sparse encoding.

    Args:
        fulltext_content: Projected fulltext string from ResourceIndexer

    Returns:
        (indices, values) tuple for SparseVector constructor.
        Returns ([], []) for empty or tokenization-free input — callers
        should skip attaching a bm25 vector in that case.
    """
    tokens = tokenize_for_search(fulltext_content)
    if not tokens:
        return [], []
    merged = tokens_to_sparse_vector(tokens, weight=1.0)
    if not merged:
        return [], []
    return list(merged.keys()), list(merged.values())


def build_query_sparse_vector(query_tokens: str) -> tuple[list[int], list[float]]:
    """Build sparse vector for a search query.

    Uses binary values (1.0) for each unique query token.
    Qdrant's BM25 modifier handles IDF scoring at query time.

    Args:
        query_tokens: Sudachi-tokenized query (space-separated)

    Returns:
        (indices, values) tuple for SparseVector constructor
    """
    if not query_tokens:
        return [], []
    unique_tokens = sorted(set(query_tokens.split()))
    indices = [mmh3.hash(t, signed=False) for t in unique_tokens]
    values = [1.0] * len(indices)
    return indices, values
