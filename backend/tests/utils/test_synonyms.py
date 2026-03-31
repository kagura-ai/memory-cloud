"""Tests for Sudachi synonym dictionary loader.

Issue #69: Verify synonym expansion for Japanese writing variations.
"""

from utils.synonyms import (
    _load_synonyms,
    expand_query_tokens,
    expand_synonyms,
)


class TestLoadSynonyms:
    """Test synonym dictionary loading."""

    def test_loads_entries(self):
        """Should load a non-empty dictionary."""
        d = _load_synonyms()
        assert len(d) > 0

    def test_groups_are_bidirectional(self):
        """If A→B then B→A."""
        d = _load_synonyms()
        # 曖昧 and あいまい should reference each other
        if "曖昧" in d:
            assert "あいまい" in d["曖昧"]
        if "あいまい" in d:
            assert "曖昧" in d["あいまい"]


class TestExpandSynonyms:
    """Test single-token synonym expansion."""

    def test_known_synonym(self):
        """Known word returns synonyms."""
        result = expand_synonyms("引越し")
        assert "引越し" in result
        assert len(result) > 1
        # Should include okurigana variations
        assert any("引っ越し" in s or "ひっこす" in s for s in result)

    def test_unknown_word(self):
        """Unknown word returns just itself."""
        result = expand_synonyms("xyzzy12345")
        assert result == ["xyzzy12345"]

    def test_empty_string(self):
        """Empty string returns itself."""
        result = expand_synonyms("")
        assert result == [""]

    def test_server_variations(self):
        """サーバー/サーバ/server should be linked."""
        result = expand_synonyms("サーバー")
        assert "サーバ" in result or "server" in result


class TestExpandQueryTokens:
    """Test multi-token query expansion."""

    def test_basic_expansion(self):
        """Multiple tokens are all expanded."""
        result = expand_query_tokens("引越し 費用")
        tokens = result.split()
        assert "引越し" in tokens
        assert len(tokens) > 2  # Expanded

    def test_empty_query(self):
        """Empty query returns empty."""
        assert expand_query_tokens("") == ""

    def test_no_duplicates(self):
        """Result should not have duplicate tokens."""
        result = expand_query_tokens("概要 要約")  # Both in same synonym group
        tokens = result.split()
        assert len(tokens) == len(set(tokens))

    def test_preserves_original(self):
        """Original tokens are always present."""
        result = expand_query_tokens("認証 エラー")
        tokens = result.split()
        assert "認証" in tokens
        assert "エラー" in tokens
