"""Sparse vector builder for Qdrant native BM25 search.

Issue #16: Converts Sudachi-tokenized text into sparse vectors
using MurmurHash3 for deterministic token-to-index mapping.

Used with Qdrant's SparseVectorParams(modifier=Modifier.IDF)
which applies IDF weighting and document length normalization
at query time (true BM25).
"""

from collections import Counter

import mmh3


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
) -> tuple[list[int], list[float]]:
    """Build combined sparse vector from all document fields.

    Summary/context_summary weighted 2x vs content to prioritize
    title-level matches over body matches.

    Args:
        summary_tokens: Sudachi-tokenized summary
        context_summary_tokens: Sudachi-tokenized context_summary
        content_tokens: Sudachi-tokenized content

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

    if not merged:
        return [], []

    # Qdrant SparseVector accepts indices in any order — no sort needed
    indices = list(merged.keys())
    values = list(merged.values())
    return indices, values


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
