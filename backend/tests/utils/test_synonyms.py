"""Tests for Sudachi synonym dictionary loader.

Issue #69: Verify synonym expansion for Japanese writing variations.
"""

import pytest

from utils.synonyms import (
    expand_query_tokens,
    expand_synonyms,
    get_synonym_dict,
)


@pytest.fixture(scope="module")
def synonym_dict():
    """Load synonym dict once for all tests (avoid repeated 2.8MB parse)."""
    return get_synonym_dict()


class TestLoadSynonyms:
    """Test synonym dictionary loading."""

    def test_loads_entries(self, synonym_dict):
        """Should load a non-empty dictionary."""
        assert len(synonym_dict) > 0

    def test_groups_are_bidirectional(self, synonym_dict):
        """If A→B then B→A."""
        if "曖昧" in synonym_dict:
            assert "あいまい" in synonym_dict["曖昧"]
        if "あいまい" in synonym_dict:
            assert "曖昧" in synonym_dict["あいまい"]


class TestExpandSynonyms:
    """Test single-token synonym expansion."""

    def test_known_synonym(self):
        """Known word returns synonyms."""
        result = expand_synonyms("引越し")
        assert "引越し" in result
        assert len(result) > 1
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
        """Server variations should be linked."""
        result = expand_synonyms("サーバー")
        assert "サーバ" in result or "server" in result


class TestExpandQueryTokens:
    """Test multi-token query expansion."""

    def test_basic_expansion(self):
        """Multiple tokens are all expanded."""
        result = expand_query_tokens("引越し 費用")
        tokens = result.split()
        assert "引越し" in tokens
        assert "費用" in tokens
        assert len(tokens) > 2

    def test_empty_query(self):
        """Empty query returns empty."""
        assert expand_query_tokens("") == ""

    def test_no_duplicates(self):
        """Result should not have duplicate tokens."""
        result = expand_query_tokens("概要 要約")
        tokens = result.split()
        assert len(tokens) == len(set(tokens))

    def test_preserves_original(self):
        """Original tokens are always present."""
        result = expand_query_tokens("認証 エラー")
        tokens = result.split()
        assert "認証" in tokens
        assert "エラー" in tokens

    def test_originals_preserved_before_cap(self):
        """All original tokens survive even when synonym cap is hit."""
        # Use many tokens that each have synonyms
        query = "概要 経緯 曖昧 宛て先 粗筋"
        result = expand_query_tokens(query)
        tokens = result.split()
        # All 5 original tokens must be present
        for original in query.split():
            assert original in tokens
