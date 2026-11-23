"""Tests for Sudachi synonym dictionary loader.

Issue #69: Verify synonym expansion for Japanese writing variations.
"""

import pytest

from utils.synonyms import (
    _load_synonyms,
    expand_query_tokens,
    expand_synonyms,
    get_synonym_dict,
)
from utils.tokenizer import normalize_term


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
        """If A→B then B→A (on normalized forms, #2986: あいまい is 曖昧)."""
        assert "不明確" in synonym_dict["曖昧"]
        assert "曖昧" in synonym_dict["不明確"]
        assert "あいまい" not in synonym_dict


class TestExpandSynonyms:
    """Test single-token synonym expansion."""

    def test_known_synonym(self):
        """Known word returns synonyms (the normalized token 引っ越し, #2986)."""
        result = expand_synonyms("引っ越し")
        assert result[0] == "引っ越し"
        assert len(result) > 1
        assert "転居" in result

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
        result = expand_query_tokens("引っ越し 費用")
        tokens = result.split()
        assert "引っ越し" in tokens
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


class TestSynonymFileRelease:
    """#2985: the file is SudachiDict v20261015's synonyms.txt."""

    def test_a_group_added_after_the_previous_copy_expands(self, synonym_dict):
        """Group 026180 (岩木山 / 津軽富士) is not in the April 2026 copy."""
        assert "津軽富士" in synonym_dict["岩木山"]
        assert "岩木山" in expand_synonyms("津軽富士")

    def test_the_file_loads_without_a_warning(self, monkeypatch):
        from utils import synonyms

        warnings: list[str] = []
        monkeypatch.setattr(synonyms.logger, "warning", lambda event, **_: warnings.append(event))
        assert len(synonyms._load_synonyms()) > 0
        assert warnings == []


def _write(tmp_path, rows: list[str]):
    path = tmp_path / "synonyms.txt"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


class TestExpansionControlFlag:
    """Column 2 of SudachiDict's synonyms.txt, the expansion-control flag.

    0 (or empty) = always used for expansion; 1 = never a trigger, but still
    an expansion of the group's other entries; 2 = never used (kept in the
    file as a deletion record).
    """

    def test_flag_0_and_empty_expand_both_ways(self, tmp_path):
        path = _write(
            tmp_path,
            ["000001,1,0,1,0,0,0,(),alpha,,", "000001,1,,2,0,0,0,(),beta,,"],
        )
        loaded = _load_synonyms(path)
        assert loaded["alpha"] == ["beta"]
        assert loaded["beta"] == ["alpha"]

    def test_flag_1_is_a_target_but_not_a_trigger(self, tmp_path):
        path = _write(
            tmp_path,
            [
                "000001,1,0,1,0,0,0,(),alpha,,",
                "000001,1,1,2,0,0,0,(),beta,,",
                "000001,1,0,3,0,0,0,(),gamma,,",
            ],
        )
        loaded = _load_synonyms(path)
        assert "beta" not in loaded
        assert loaded["alpha"] == ["beta", "gamma"]
        assert loaded["gamma"] == ["alpha", "beta"]

    def test_flag_2_is_never_used(self, tmp_path):
        path = _write(
            tmp_path,
            [
                "000001,1,0,1,0,0,0,(),alpha,,",
                "000001,1,2,2,0,0,0,(),beta,,",
                "000001,1,0,3,0,0,0,(),gamma,,",
            ],
        )
        loaded = _load_synonyms(path)
        assert "beta" not in loaded
        assert loaded["alpha"] == ["gamma"]
        assert loaded["gamma"] == ["alpha"]

    def test_a_group_with_one_usable_entry_expands_nothing(self, tmp_path):
        path = _write(
            tmp_path,
            ["000001,1,0,1,0,0,0,(),alpha,,", "000001,1,2,2,0,0,0,(),beta,,"],
        )
        assert _load_synonyms(path) == {}

    def test_a_group_of_targets_only_has_no_trigger(self, tmp_path):
        path = _write(
            tmp_path,
            ["000001,1,1,1,0,0,0,(),alpha,,", "000001,1,1,2,0,0,0,(),beta,,"],
        )
        assert _load_synonyms(path) == {}

    def test_the_flag_is_per_group(self, tmp_path):
        """A term that is only a target in one group is a trigger in another."""
        path = _write(
            tmp_path,
            [
                "000001,1,0,1,0,0,0,(),alpha,,",
                "000001,1,1,2,0,0,0,(),beta,,",
                "",
                "000002,1,0,1,0,0,0,(),beta,,",
                "000002,1,0,2,0,0,0,(),delta,,",
            ],
        )
        loaded = _load_synonyms(path)
        assert loaded["beta"] == ["delta"]
        assert loaded["alpha"] == ["beta"]
        assert loaded["delta"] == ["beta"]

    def test_the_shipped_file(self, synonym_dict):
        """SudachiDict's own flags: ``sub`` is flag 2 in its only group,
        ``abstract`` flag 1 in both of its groups, ``yellow`` flag 2 next to
        イエローカード and flag 1 elsewhere."""
        assert "sub" not in synonym_dict
        assert "abstract" not in synonym_dict
        assert "abstract" in synonym_dict[normalize_term("概要")]
        assert "yellow" not in synonym_dict
        assert "yellow" not in synonym_dict[normalize_term("イエローカード")]
        assert "sub" not in expand_query_tokens(normalize_term("副")).split()
