"""Tests for Japanese tokenizer (Issue #1)."""

from utils.tokenizer import tokenize_for_search


class TestTokenizeForSearch:
    """Test tokenize_for_search utility."""

    def test_empty_string(self):
        assert tokenize_for_search("") == ""

    def test_none_like(self):
        assert tokenize_for_search("") == ""

    def test_english_passthrough(self):
        result = tokenize_for_search("Fixed authentication error in JWT")
        assert result == "fixed authentication error in jwt"

    def test_japanese_lemmatization(self):
        result = tokenize_for_search("Pythonの認証エラーを修正した")
        tokens = result.split()
        assert "python" in tokens
        assert "認証" in tokens
        assert "エラー" in tokens
        assert "修正" in tokens
        assert "する" in tokens  # 修正した → 修正 + する (lemmatized)

    def test_japanese_verb_conjugation(self):
        result = tokenize_for_search("走ったことがある")
        tokens = result.split()
        assert "走る" in tokens  # 走った → 走る
        assert "こと" in tokens
        assert "ある" in tokens

    def test_stop_words_removed(self):
        result = tokenize_for_search("データベースのテーブルを変更した")
        tokens = result.split()
        # Particles should be excluded
        assert "の" not in tokens
        assert "を" not in tokens
        assert "た" not in tokens
        # Content words should remain
        assert "データベース" in tokens
        assert "テーブル" in tokens
        assert "変更" in tokens

    def test_mixed_japanese_english(self):
        result = tokenize_for_search("JWTの認証エラーをfixした")
        tokens = result.split()
        assert "jwt" in tokens
        assert "認証" in tokens
        assert "エラー" in tokens
        assert "fix" in tokens

    def test_katakana(self):
        result = tokenize_for_search("サーバーを構築した")
        tokens = result.split()
        assert "サーバー" in tokens
        assert "構築" in tokens
