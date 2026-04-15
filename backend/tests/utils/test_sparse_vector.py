"""Tests for sparse vector builder utility.

Issue #16: Verify MurmurHash3-based sparse vector construction.
"""

import mmh3

from utils.sparse_vector import (
    build_document_sparse_vector,
    build_query_sparse_vector,
    build_resource_sparse_vector,
    tokens_to_sparse_vector,
)


class TestTokensToSparseVector:
    """Test tokens_to_sparse_vector."""

    def test_basic(self):
        """Single token produces one index with TF=1."""
        result = tokens_to_sparse_vector("hello")
        assert len(result) == 1
        idx = mmh3.hash("hello", signed=False)
        assert result[idx] == 1.0

    def test_repeated_token(self):
        """Repeated token increases TF count."""
        result = tokens_to_sparse_vector("hello hello hello")
        idx = mmh3.hash("hello", signed=False)
        assert result[idx] == 3.0

    def test_multiple_tokens(self):
        """Multiple unique tokens produce separate indices."""
        result = tokens_to_sparse_vector("hello world")
        assert len(result) == 2

    def test_weight_multiplier(self):
        """Weight multiplies TF values."""
        result = tokens_to_sparse_vector("hello", weight=2.0)
        idx = mmh3.hash("hello", signed=False)
        assert result[idx] == 2.0

    def test_empty_string(self):
        """Empty string returns empty dict."""
        assert tokens_to_sparse_vector("") == {}

    def test_japanese_tokens(self):
        """Japanese tokens hash correctly."""
        result = tokens_to_sparse_vector("認証 エラー 解決")
        assert len(result) == 3

    def test_deterministic(self):
        """Same input always produces same output."""
        r1 = tokens_to_sparse_vector("hello world")
        r2 = tokens_to_sparse_vector("hello world")
        assert r1 == r2


class TestBuildDocumentSparseVector:
    """Test build_document_sparse_vector."""

    def test_basic(self):
        """Produces indices and values of equal length."""
        indices, values = build_document_sparse_vector("hello", "", "")
        assert len(indices) > 0
        assert len(values) == len(indices)

    def test_summary_weighted_2x(self):
        """Summary tokens get 2x weight."""
        indices, values = build_document_sparse_vector("test", "", "")
        idx = mmh3.hash("test", signed=False)
        pos = indices.index(idx)
        assert values[pos] == 2.0  # weight=2.0 for summary

    def test_content_weighted_1x(self):
        """Content tokens get 1x weight."""
        indices, values = build_document_sparse_vector("", "", "test")
        idx = mmh3.hash("test", signed=False)
        pos = indices.index(idx)
        assert values[pos] == 1.0  # weight=1.0 for content

    def test_merged_fields(self):
        """Same token in summary and content gets combined weight."""
        indices, values = build_document_sparse_vector("test", "", "test")
        idx = mmh3.hash("test", signed=False)
        pos = indices.index(idx)
        assert values[pos] == 3.0  # 2.0 (summary) + 1.0 (content)

    def test_all_empty(self):
        """All empty returns empty lists."""
        indices, values = build_document_sparse_vector("", "", "")
        assert indices == []
        assert values == []


class TestBuildResourceSparseVector:
    """Test build_resource_sparse_vector (Issue #335)."""

    def test_basic(self):
        """Non-empty content yields equal-length indices/values."""
        indices, values = build_resource_sparse_vector("hello world")
        assert len(indices) > 0
        assert len(values) == len(indices)

    def test_weight_1x(self):
        """Resource tokens are weighted at 1.0 to match memory-side `content`."""
        indices, values = build_resource_sparse_vector("test")
        idx = mmh3.hash("test", signed=False)
        pos = indices.index(idx)
        assert values[pos] == 1.0

    def test_empty(self):
        """Empty string returns empty lists (caller skips bm25 attach)."""
        indices, values = build_resource_sparse_vector("")
        assert indices == []
        assert values == []

    def test_japanese_content(self):
        """Japanese fulltext is tokenized via Sudachi before hashing."""
        indices, values = build_resource_sparse_vector("認証エラーの解決方法")
        assert len(indices) > 0
        assert all(v > 0 for v in values)

    def test_deterministic(self):
        """Same input produces identical output (idempotency for re-index)."""
        a = build_resource_sparse_vector("PostgreSQL migration 戦略")
        b = build_resource_sparse_vector("PostgreSQL migration 戦略")
        assert a == b


class TestBuildQuerySparseVector:
    """Test build_query_sparse_vector."""

    def test_basic(self):
        """Query tokens get binary 1.0 values."""
        indices, values = build_query_sparse_vector("hello world")
        assert len(indices) == 2
        assert all(v == 1.0 for v in values)

    def test_deduplication(self):
        """Repeated query tokens are deduplicated."""
        indices, values = build_query_sparse_vector("hello hello")
        assert len(indices) == 1

    def test_empty(self):
        """Empty query returns empty lists."""
        indices, values = build_query_sparse_vector("")
        assert indices == []
        assert values == []

    def test_indices_length(self):
        """Indices match unique token count."""
        indices, _ = build_query_sparse_vector("z a m")
        assert len(indices) == 3

    def test_japanese_query(self):
        """Japanese query tokens work."""
        indices, values = build_query_sparse_vector("認証 エラー")
        assert len(indices) == 2
        assert all(v == 1.0 for v in values)
