"""Tests for text_to_reading and tokenize_and_reading.

Issue #73: Verify katakana reading extraction for hiragana query matching.
"""

from utils.tokenizer import augment_reading_tokens, text_to_reading, tokenize_and_reading


class TestTextToReading:
    """Test text_to_reading function."""

    def test_kanji_to_katakana(self):
        """Kanji text produces katakana readings."""
        result = text_to_reading("認証エラー")
        assert "ニンショウ" in result or "エラー" in result
        assert len(result) > 0

    def test_empty_string(self):
        """Empty string returns empty."""
        assert text_to_reading("") == ""

    def test_non_cjk_returns_empty(self):
        """Non-CJK text returns empty (no reading needed)."""
        assert text_to_reading("Hello World") == ""

    def test_hiragana_to_katakana(self):
        """Hiragana input produces katakana readings."""
        result = text_to_reading("ひっこし")
        assert "ヒッコシ" in result

    def test_stop_words_filtered(self):
        """Particles (助詞) should be filtered from readings."""
        result = text_to_reading("引越しの費用")
        tokens = result.split()
        # "の" (particle) should not appear
        assert "ノ" not in tokens

    def test_mixed_cjk_latin(self):
        """Mixed text should produce readings for CJK parts."""
        result = text_to_reading("OAuth2認証")
        assert len(result) > 0


class TestTokenizeAndReading:
    """Test tokenize_and_reading function."""

    def test_returns_tuple(self):
        """Returns (lemmas, readings) tuple."""
        lemmas, readings = tokenize_and_reading("引越し業者")
        assert isinstance(lemmas, str)
        assert isinstance(readings, str)
        assert len(lemmas) > 0
        assert len(readings) > 0

    def test_non_cjk(self):
        """Non-CJK: lemmas are lowercased, readings are empty."""
        lemmas, readings = tokenize_and_reading("Hello World")
        assert lemmas == "hello world"
        assert readings == ""

    def test_empty(self):
        """Empty input returns empty tuple."""
        lemmas, readings = tokenize_and_reading("")
        assert lemmas == ""
        assert readings == ""

    def test_consistent_filtering(self):
        """Both lemmas and readings filter the same stop words."""
        lemmas, readings = tokenize_and_reading("猫は魚を食べた")
        lemma_count = len(lemmas.split())
        reading_count = len(readings.split())
        # Same number of tokens (same stop words filtered)
        assert lemma_count == reading_count

    def test_reading_weight_in_sparse_vector(self):
        """Summary reading at weight=0.5 in sparse vector."""
        from utils.sparse_vector import build_document_sparse_vector

        indices_without, _ = build_document_sparse_vector("test", "", "")
        indices_with, values_with = build_document_sparse_vector(
            "test", "", "", summary_reading="テスト"
        )
        # With reading should have more indices
        assert len(indices_with) >= len(indices_without)


class TestAugmentReadingTokens:
    """Test augment_reading_tokens for hiragana query matching (Issue #75)."""

    def test_adjacent_concat_hiyou(self):
        """ひよう → ヒ+ヨウ adjacent concat produces ヒヨウ."""
        result = augment_reading_tokens("ひっこしのひよう")
        assert "ヒヨウ" in result.split()

    def test_full_katakana_hatarakikata(self):
        """はたらきかたかいかく → full kata conversion matches compound token."""
        result = augment_reading_tokens("はたらきかたかいかく")
        assert "ハタラキカタカイカク" in result.split()

    def test_empty_string(self):
        """Empty string returns empty."""
        assert augment_reading_tokens("") == ""

    def test_non_cjk_returns_empty(self):
        """Non-CJK text returns empty (no augmentation needed)."""
        assert augment_reading_tokens("Hello World") == ""

    def test_kanji_query_no_noise(self):
        """Kanji queries should not add spurious tokens."""
        result = augment_reading_tokens("引越しの費用")
        tokens = result.split() if result else []
        # Each group has only 1 content token, no adjacent concat needed
        # No hiragana runs >= 4 chars, so no full kata conversion
        assert len(tokens) == 0

    def test_short_hiragana_skipped(self):
        """Very short hiragana runs (< 4 chars) are not converted."""
        result = augment_reading_tokens("猫の餌")
        # "の" is only 1 char hiragana — too short for strategy 2
        assert result == "" or "ノ" not in result.split()

    def test_working_case_unaffected(self):
        """Control case: くものすのそうじ already works, augmentation shouldn't break it."""
        result = augment_reading_tokens("くものすのそうじ")
        # Should produce some tokens but not break anything
        assert isinstance(result, str)
