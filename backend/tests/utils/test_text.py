"""Tests for text normalization utilities.

Issue #163: Verify Unicode normalization for consistent search.
Issue #173: Verify size limit protection against DoS attacks.
"""

import pytest

from utils.text import detect_symbol_density, normalize_for_search


class TestNormalizeForSearch:
    """Tests for normalize_for_search function."""

    def test_none_input(self):
        """Should return None for None input."""
        assert normalize_for_search(None) is None

    def test_empty_string(self):
        """Should return empty string for empty input."""
        assert normalize_for_search("") == ""

    def test_ascii_unchanged(self):
        """ASCII text should be unchanged."""
        assert normalize_for_search("Hello World") == "Hello World"

    def test_nfkc_halfwidth_to_fullwidth_katakana(self):
        """Half-width katakana should be converted to full-width."""
        # ｶﾀｶﾅ (half-width) → カタカナ (full-width)
        assert normalize_for_search("ｶﾀｶﾅ") == "カタカナ"

    def test_nfkc_halfwidth_mixed(self):
        """Mixed half-width/full-width should be normalized."""
        # ﾃｽﾄテスト (mixed) → テストテスト (all full-width)
        assert normalize_for_search("ﾃｽﾄテスト") == "テストテスト"

    def test_nfkc_circled_numbers(self):
        """Circled numbers should be converted to regular digits."""
        # ① → 1, ② → 2, ③ → 3
        assert normalize_for_search("①②③") == "123"

    def test_nfkc_circled_letters(self):
        """Circled letters should be converted."""
        # Ⓐ → A
        assert normalize_for_search("Ⓐ") == "A"

    def test_nfkc_fullwidth_ascii(self):
        """Full-width ASCII should be converted to half-width."""
        # ａｂｃ → abc
        assert normalize_for_search("ａｂｃ") == "abc"

    def test_nfkc_japanese_company_symbol(self):
        """Japanese company symbols should be expanded."""
        # ㈱ → (株)
        assert normalize_for_search("㈱会社") == "(株)会社"

    def test_nfc_composed_hiragana(self):
        """Decomposed hiragana with dakuten should be composed."""
        # か + ゛ (2 codepoints) → が (1 codepoint)
        nfd_ga = "\u304b\u3099"  # か + combining dakuten
        assert normalize_for_search(nfd_ga) == "が"

    def test_nfc_composed_katakana(self):
        """Decomposed katakana with dakuten should be composed."""
        # カ + ゛ (2 codepoints) → ガ (1 codepoint)
        nfd_ga = "\u30ab\u3099"  # カ + combining dakuten
        assert normalize_for_search(nfd_ga) == "ガ"

    def test_japanese_unchanged(self):
        """Normal Japanese text should be unchanged."""
        assert normalize_for_search("認証エラー解決") == "認証エラー解決"

    def test_mixed_japanese_ascii(self):
        """Mixed Japanese and ASCII should work."""
        assert normalize_for_search("OAuth2認証") == "OAuth2認証"

    def test_preserves_spaces(self):
        """Spaces should be preserved."""
        assert normalize_for_search("Hello World") == "Hello World"

    def test_preserves_newlines(self):
        """Newlines should be preserved."""
        assert normalize_for_search("Hello\nWorld") == "Hello\nWorld"

    def test_size_limit_within_bounds(self):
        """Text within size limit should be processed normally.

        Issue #173: Verify normal operation under size limit.
        """
        # Create text well under 100KB limit
        text = "Hello World" * 100  # ~1.1KB
        result = normalize_for_search(text)
        assert result is not None
        assert len(result) > 0

    def test_size_limit_exceeded(self):
        """Text exceeding size limit should raise ValueError.

        Issue #173: Verify DoS protection via size limit.
        """
        # Create text over 100KB limit (default)
        # Using a simple ASCII character (1 byte each)
        large_text = "a" * 200000  # 200KB

        with pytest.raises(ValueError, match="exceeds maximum allowed"):
            normalize_for_search(large_text)

    def test_size_limit_exactly_at_boundary(self):
        """Text exactly at size limit should be processed.

        Issue #173: Verify boundary condition.
        """
        from utils.text import MAX_NORMALIZE_SIZE

        # Create text exactly at the limit
        text = "a" * MAX_NORMALIZE_SIZE
        result = normalize_for_search(text)
        assert result is not None

    def test_size_limit_multibyte_characters(self):
        """Size limit should account for UTF-8 byte size, not character count.

        Issue #173: Verify proper byte size calculation for multibyte chars.
        """
        # Japanese characters are 3 bytes each in UTF-8
        # 35000 characters * 3 bytes = 105000 bytes > 100KB limit
        large_japanese_text = "あ" * 35000

        with pytest.raises(ValueError, match="exceeds maximum allowed"):
            normalize_for_search(large_japanese_text)


class TestDetectSymbolDensity:
    """Tests for detect_symbol_density function."""

    def test_empty_string(self):
        """Empty string should return False."""
        assert detect_symbol_density("") is False

    def test_pure_text(self):
        """Pure text should have low symbol density."""
        assert detect_symbol_density("Hello World") is False

    def test_url(self):
        """URLs should have high symbol density."""
        assert detect_symbol_density("https://example.com/path?q=1") is True

    def test_code_snippet(self):
        """Code snippets should have high symbol density."""
        assert detect_symbol_density("C++ node.js AWS_S3") is True

    def test_email(self):
        """Email addresses should have high symbol density."""
        assert detect_symbol_density("user@example.com") is True

    def test_japanese_text(self):
        """Japanese text without symbols should have low density."""
        assert detect_symbol_density("認証エラー解決") is False

    def test_custom_threshold(self):
        """Custom threshold should work."""
        # "a:b" has 25% symbols (1 out of 4 non-space chars)
        assert detect_symbol_density("a:b", threshold=0.2) is True
        assert detect_symbol_density("a:b", threshold=0.3) is False

    def test_programming_symbols(self):
        """Programming symbols should be detected."""
        assert detect_symbol_density("function() { return; }") is True

    def test_date_format(self):
        """Date formats should have moderate density."""
        # "2025-12-05" has 20% symbols (2 out of 10)
        assert detect_symbol_density("2025-12-05", threshold=0.15) is True
        assert detect_symbol_density("2025-12-05", threshold=0.25) is False

    def test_invalid_threshold_too_low(self):
        """Threshold below 0.0 should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
            detect_symbol_density("test", threshold=-0.1)

    def test_invalid_threshold_too_high(self):
        """Threshold above 1.0 should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
            detect_symbol_density("test", threshold=1.5)

    def test_boundary_thresholds(self):
        """Boundary values 0.0 and 1.0 should be valid."""
        # Should not raise
        detect_symbol_density("test", threshold=0.0)
        detect_symbol_density("test", threshold=1.0)
