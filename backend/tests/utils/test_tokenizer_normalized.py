"""#2986: keyword tokens use Sudachi's normalized form, so spelling variants agree.

Documents (``tokenize_and_reading`` / ``tokenize_for_search``, the point
builders) and queries (``build_fulltext_query``) share the lemma function.
Readings and non-CJK text are unchanged; a Latin-script word inside Japanese
text keeps its dictionary form, as the same word in an English query has it.
"""

from __future__ import annotations

import pytest

from utils.synonyms import expand_query_tokens, expand_synonyms, get_synonym_dict
from utils.tokenizer import (
    build_fulltext_query,
    normalize_term,
    tokenize_and_reading,
    tokenize_for_search,
)

VARIANT_PAIRS = [
    ("引越し", "引っ越し"),
    ("届出", "届け出"),
    ("附属", "付属"),
    ("サーバ", "サーバー"),
    ("打合せ", "打ち合わせ"),
]


@pytest.mark.parametrize(("one", "other"), VARIANT_PAIRS)
def test_a_variant_pair_is_one_document_token(one, other):
    assert tokenize_for_search(one) == tokenize_for_search(other)
    assert tokenize_and_reading(one)[0] == tokenize_and_reading(other)[0]
    assert len(tokenize_for_search(one).split()) == 1


@pytest.mark.parametrize(("one", "other"), VARIANT_PAIRS)
def test_a_query_in_one_spelling_carries_the_document_token_of_the_other(one, other):
    document = tokenize_for_search(f"{other}の件")
    query = build_fulltext_query(f"{one}について").split()
    for token in document.split():
        if token != tokenize_for_search("件"):
            assert token in query


def test_inflection_is_still_lemmatized():
    assert tokenize_for_search("走った") == "走る"


def test_readings_are_unchanged():
    assert tokenize_and_reading("引越し")[1] == tokenize_and_reading("引越し")[1] != ""
    assert tokenize_and_reading("サーバ")[1] == "サーバ"


def test_non_cjk_text_is_unchanged():
    assert tokenize_for_search("Server Migration") == "server migration"
    assert tokenize_and_reading("Server")[0] == "server"


def test_a_latin_word_inside_japanese_text_keeps_its_dictionary_form():
    """Otherwise "Server" in a Japanese memory would index as サーバー and the
    English query "server" (not tokenized by Sudachi) would miss it."""
    assert tokenize_for_search("Server の設定").split()[0] == "server"


def test_normalize_term():
    assert normalize_term("引越し") == "引っ越し"
    assert normalize_term("Server") == "server"
    assert normalize_term("") == ""


class TestSynonymsOnNormalizedTokens:
    def test_the_dictionary_is_keyed_by_normalized_forms(self):
        synonyms = get_synonym_dict()
        assert "引っ越し" in synonyms
        assert "引越し" not in synonyms
        for others in list(synonyms.values())[:2000]:
            assert len(others) == len(set(others))

    def test_expansions_are_normalized_and_exclude_the_token(self):
        expanded = expand_synonyms("引っ越し")
        assert expanded[0] == "引っ越し"
        assert "引っ越し" not in expanded[1:]
        assert "引越し" not in expanded
        assert len(expanded) > 1
        for synonym in expanded[1:]:
            assert normalize_term(synonym) == synonym

    def test_a_variant_pair_that_is_a_synonym_group_expands_once(self):
        query = build_fulltext_query("引越し 引っ越し").split()
        assert query.count("引っ越し") == 1
        assert len(query) == len(set(query))
        assert "引越し" not in query
        assert expand_query_tokens("引っ越し 引っ越し").split().count("引っ越し") == 1
