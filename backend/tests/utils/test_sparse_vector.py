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


# ---------------------------------------------------------------------------
# #2984: Okapi BM25 term saturation and length normalisation (document side)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from utils.sparse_vector import (  # noqa: E402
    BM25_B,
    BM25_K1,
    MemoryFieldStats,
    bm25_field_vector,
    token_count,
)


def _bm25(tf: int, dl: int, avgdl: float, weight: float = 1.0) -> float:
    norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
    return weight * tf * (BM25_K1 + 1) / (tf + norm)


def _value(vector: tuple[list[int], list[float]], token: str) -> float:
    indices, values = vector
    return dict(zip(indices, values, strict=True))[mmh3.hash(token, signed=False)]


class TestBm25Constants:
    def test_textbook_constants(self):
        assert BM25_K1 == 1.2
        assert BM25_B == 0.75


class TestBm25FieldVector:
    def test_exact_formula(self):
        """tf 2 in a 4-token field whose context average is 8."""
        out = bm25_field_vector("a a b c", 2.0, 8.0)
        assert out[mmh3.hash("a", signed=False)] == pytest.approx(_bm25(2, 4, 8.0, 2.0))
        assert out[mmh3.hash("b", signed=False)] == pytest.approx(_bm25(1, 4, 8.0, 2.0))

    def test_saturation_is_sublinear_and_bounded(self):
        """Repeating a term raises its value less and less, never past w * (k1 + 1)."""
        values = [bm25_field_vector(" ".join(["x"] * tf), 1.0, 10.0) for tf in (1, 2, 4, 50)]
        scores = [v[mmh3.hash("x", signed=False)] for v in values]
        assert scores == sorted(scores)
        assert scores[1] - scores[0] > scores[2] - scores[1] > 0
        assert scores[-1] < BM25_K1 + 1

    def test_length_normalisation(self):
        """The same single match weighs less in a longer field than in a shorter one."""
        short = bm25_field_vector("x " + " ".join(f"s{i}" for i in range(1)), 1.0, 10.0)
        long = bm25_field_vector("x " + " ".join(f"l{i}" for i in range(39)), 1.0, 10.0)
        idx = mmh3.hash("x", signed=False)
        assert long[idx] < short[idx]

    def test_average_length_field_with_one_match_keeps_the_field_weight(self):
        """dl == avgdl and tf 1: the BM25 factor is exactly 1."""
        out = bm25_field_vector("x y z", 2.0, 3.0)
        assert out[mmh3.hash("x", signed=False)] == pytest.approx(2.0)

    @pytest.mark.parametrize("avgdl", [None, 0.0, -1.0])
    def test_no_statistics_falls_back_to_saturation_only(self, avgdl):
        """No usable average: avgdl = dl, so only the saturation applies."""
        out = bm25_field_vector("x x y", 1.0, avgdl)
        assert out[mmh3.hash("x", signed=False)] == pytest.approx(_bm25(2, 3, 3.0))
        assert out[mmh3.hash("y", signed=False)] == pytest.approx(_bm25(1, 3, 3.0))

    def test_empty_field_adds_nothing(self):
        assert bm25_field_vector("", 2.0, 5.0) == {}
        assert bm25_field_vector("   ", 2.0, 5.0) == {}

    def test_token_count(self):
        assert token_count("") == 0
        assert token_count("a b  a") == 3


class TestDocumentVectorWithStatistics:
    def test_field_weights_and_per_field_averages(self):
        stats = MemoryFieldStats(
            summary=4.0, context_summary=10.0, content=100.0, summary_reading=6.0
        )
        vector = build_document_sparse_vector("s s t u", "c d", "b " * 49 + "b", "r", stats=stats)
        assert _value(vector, "s") == pytest.approx(_bm25(2, 4, 4.0, 2.0))
        assert _value(vector, "c") == pytest.approx(_bm25(1, 2, 10.0, 2.0))
        assert _value(vector, "b") == pytest.approx(_bm25(50, 50, 100.0, 1.0))
        assert _value(vector, "r") == pytest.approx(_bm25(1, 1, 6.0, 0.5))

    def test_a_term_in_two_fields_sums_its_field_values(self):
        stats = MemoryFieldStats(summary=2.0, context_summary=None, content=8.0)
        vector = build_document_sparse_vector("x y", "", "x a b c", stats=stats)
        assert _value(vector, "x") == pytest.approx(_bm25(1, 2, 2.0, 2.0) + _bm25(1, 4, 8.0))

    def test_a_field_without_an_average_uses_its_own_length(self):
        stats = MemoryFieldStats(summary=None, context_summary=None, content=None)
        vector = build_document_sparse_vector("x x y", "", "", stats=stats)
        assert _value(vector, "x") == pytest.approx(_bm25(2, 3, 3.0, 2.0))

    def test_without_statistics_equals_empty_statistics(self):
        args = ("a a b", "c", "d d d e", "r r")
        assert build_document_sparse_vector(*args) == build_document_sparse_vector(
            *args, stats=MemoryFieldStats()
        )

    def test_longer_document_ranks_below_shorter_for_one_match(self):
        """The #2984 failure: a long memory no longer outranks a short one by length."""
        stats = MemoryFieldStats(summary=5.0, context_summary=None, content=20.0)
        short = build_document_sparse_vector("q a b c d", "", "", stats=stats)
        long = build_document_sparse_vector(
            "q a b c d", "", " ".join(["q"] * 10 + [f"w{i}" for i in range(190)]), stats=stats
        )
        long_without = build_document_sparse_vector(
            "q a b c d", "", " ".join(["q"] * 10 + [f"w{i}" for i in range(190)])
        )
        # Saturated and length-normalised: ten repeats in a body ten times the
        # context's average add about one single match, not ten (raw tf).
        content_part = _value(long, "q") - _value(short, "q")
        assert content_part == pytest.approx(_bm25(10, 200, 20.0))
        assert 0 < content_part < 0.2 * 10
        assert _value(long_without, "q") > _value(long, "q")


class TestResourceVectorWithStatistics:
    def test_fulltext_average(self):
        vector = build_resource_sparse_vector("alpha alpha beta", avgdl=12.0)
        assert _value(vector, "alpha") == pytest.approx(_bm25(2, 3, 12.0))

    def test_precomputed_tokens_are_used_as_given(self):
        assert build_resource_sparse_vector(
            "ignored", avgdl=4.0, tokens="x y y"
        ) == build_resource_sparse_vector("ignored too", avgdl=4.0, tokens="x y y")

    def test_no_average_is_saturation_only(self):
        vector = build_resource_sparse_vector("alpha alpha beta")
        assert _value(vector, "alpha") == pytest.approx(_bm25(2, 3, 3.0))
