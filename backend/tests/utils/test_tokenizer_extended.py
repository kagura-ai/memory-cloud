"""Extended tests for tokenizer edge cases.

Issue #14: Increase unit test coverage — tokenizer edge cases.
"""

from utils.tokenizer import tokenize_for_search


class TestTokenizerEdgeCases:
    """Extended edge case tests for tokenize_for_search."""

    def test_empty_string(self):
        """Empty string returns empty."""
        assert tokenize_for_search("") == ""

    def test_english_lowercase(self):
        """English text returned lowercase."""
        assert tokenize_for_search("Hello World") == "hello world"

    def test_pure_ascii_passthrough(self):
        """ASCII-only text passes through with lowercasing."""
        result = tokenize_for_search("Python FastAPI PostgreSQL")
        assert result == "python fastapi postgresql"

    def test_numbers_only(self):
        """Numeric strings pass through."""
        assert tokenize_for_search("12345") == "12345"

    def test_special_characters(self):
        """Special characters pass through (no CJK)."""
        result = tokenize_for_search("user@example.com")
        assert result == "user@example.com"

    def test_unicode_non_cjk(self):
        """Non-CJK unicode (e.g. emoji, accented chars) passes through."""
        result = tokenize_for_search("café résumé")
        assert result == "café résumé"

    def test_japanese_basic(self):
        """Japanese text is tokenized with Sudachi."""
        result = tokenize_for_search("認証エラーを修正する")
        assert len(result) > 0
        assert " " in result  # Should be space-separated tokens

    def test_japanese_verb_conjugation(self):
        """Japanese verbs should be lemmatized to dictionary form."""
        # "走った" (ran, past tense) → "走る" (run, dictionary form)
        result = tokenize_for_search("走った")
        assert "走る" in result

    def test_japanese_stop_words_removed(self):
        """Japanese stop words (particles, aux verbs) should be removed."""
        # "は", "を", "が" are particles → should be filtered
        result = tokenize_for_search("猫は魚を食べた")
        assert "は" not in result.split()
        assert "を" not in result.split()

    def test_mixed_japanese_english(self):
        """Mixed text should handle both scripts."""
        result = tokenize_for_search("OAuth2認証エラー")
        assert len(result) > 0

    def test_katakana(self):
        """Katakana text should be tokenized."""
        result = tokenize_for_search("テストケース")
        assert len(result) > 0

    def test_long_text(self):
        """Long text should not fail."""
        long_text = "テスト " * 100
        result = tokenize_for_search(long_text)
        assert len(result) > 0

    def test_whitespace_only(self):
        """Whitespace-only returns empty (no CJK detected)."""
        result = tokenize_for_search("   ")
        assert result == "   "  # Lowercased whitespace = same

    def test_single_character_japanese(self):
        """Single Japanese character works."""
        result = tokenize_for_search("猫")
        assert len(result) > 0

    def test_programming_terms_english(self):
        """Programming terms in English pass through."""
        result = tokenize_for_search("async def get_user(user_id: str)")
        assert "async" in result
        assert "def" in result
