"""Japanese-aware tokenizer for BM25 search.

Uses Sudachi for morphological analysis with lemmatization.
Falls back to simple word splitting for non-CJK text.
"""

import re
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# CJK Unicode ranges: Hiragana, Katakana, CJK Unified Ideographs
_CJK_PATTERN = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")

# Parts of speech to exclude (functional words with low search value)
_STOP_POS = frozenset({"助詞", "助動詞", "補助記号", "空白"})

# Lazy-loaded Sudachi tokenizer
_sudachi_tokenizer = None


def _get_sudachi():
    global _sudachi_tokenizer
    if _sudachi_tokenizer is None:
        from sudachipy import Dictionary

        _sudachi_tokenizer = Dictionary().create()
    return _sudachi_tokenizer


def _sudachi_extract(
    text: str, extractor, error_label: str = "tokenization", fallback: str | None = None
) -> str:
    """Shared Sudachi extraction: tokenize, filter stop words, extract attribute.

    Args:
        text: Input CJK text
        extractor: Function to call on each token (e.g., lambda t: t.dictionary_form())
        error_label: Label for error logging
        fallback: Value to return on error (default: text.lower() for search, "" for reading)

    Returns:
        Space-separated extracted tokens
    """
    try:
        tokenizer = _get_sudachi()
        tokens = tokenizer.tokenize(text)
        result = []
        for token in tokens:
            if token.part_of_speech()[0] in _STOP_POS:
                continue
            result.append(extractor(token))
        return " ".join(result)
    except Exception as e:
        logger.warning(
            f"sudachi_{error_label}_failed", text_length=len(text), error=str(e), exc_info=True
        )
        return fallback if fallback is not None else text.lower()


def tokenize_and_reading(text: str) -> tuple[str, str, Any | None]:
    """Tokenize text and extract readings in a single Sudachi pass.

    Issue #73: Avoids double tokenization when both lemmas and readings
    are needed (e.g., in BM25 search query building).

    Args:
        text: Input text

    Returns:
        (lemma_tokens, reading_tokens, raw_sudachi_tokens) tuple.
        Lemmas/readings are space-separated. raw_sudachi_tokens is the
        Sudachi morpheme list for reuse by augment_reading_tokens.
        For non-CJK text: (lowercased text, "", None)
    """
    if not text:
        return "", "", None

    if not _CJK_PATTERN.search(text):
        return text.lower(), "", None

    try:
        tokenizer = _get_sudachi()
        tokens = tokenizer.tokenize(text)
        lemmas = []
        readings = []
        for token in tokens:
            if token.part_of_speech()[0] in _STOP_POS:
                continue
            lemmas.append(token.dictionary_form().lower())
            readings.append(token.reading_form())
        return " ".join(lemmas), " ".join(readings), tokens
    except Exception as e:
        logger.warning(
            "sudachi_tokenization_failed",
            text_length=len(text),
            error=str(e),
            exc_info=True,
        )
        return text.lower(), "", None


def text_to_reading(text: str) -> str:
    """Convert text to space-separated katakana readings using Sudachi.

    Issue #73: For BM25 matching of full-hiragana queries (voice input, IME).

    Args:
        text: Input text (any script)

    Returns:
        Space-separated katakana reading tokens. Empty for non-CJK text.
    """
    if not text or not _CJK_PATTERN.search(text):
        return ""

    return _sudachi_extract(text, lambda t: t.reading_form(), "reading", fallback="")


_HIRAGANA_PATTERN = re.compile(r"[\u3041-\u3096]+")
_HIRA_TO_KATA = str.maketrans({chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)})
_MIN_HIRAGANA_RUN = 4


def augment_reading_tokens(text: str, sudachi_tokens: Any | None = None) -> str:
    """Generate additional reading tokens for hiragana query matching.

    Issue #75: Sudachi mis-segments continuous hiragana, producing different
    morpheme boundaries than the corresponding kanji text. Two strategies:

    1. Adjacent reading concatenation: group content tokens between stop-word
       boundaries and concatenate their readings. Fixes cases like
       ひよう → ヒ+ヨウ → ヒヨウ (matches doc's 費用 reading).

    2. Full hiragana→katakana conversion: convert contiguous hiragana runs
       directly to katakana without Sudachi. Fixes compound-word cases like
       はたらきかたかいかく → ハタラキカタカイカク (matches doc's 働き方改革).

    Args:
        text: Input query text
        sudachi_tokens: Pre-tokenized Sudachi morpheme list (avoids double tokenization)

    Returns:
        Space-separated additional tokens (may be empty)
    """
    if not text or not _HIRAGANA_PATTERN.search(text):
        return ""

    extra: list[str] = []

    try:
        tokens = sudachi_tokens if sudachi_tokens is not None else _get_sudachi().tokenize(text)
        group: list[str] = []
        for token in tokens:
            if token.part_of_speech()[0] in _STOP_POS:
                if len(group) >= 2:
                    extra.append("".join(group))
                group = []
            else:
                group.append(token.reading_form())
        if len(group) >= 2:
            extra.append("".join(group))
    except Exception as e:
        logger.warning("augment_reading_concat_failed", error=str(e), exc_info=True)

    for match in _HIRAGANA_PATTERN.finditer(text):
        hira_run = match.group()
        if len(hira_run) >= _MIN_HIRAGANA_RUN:
            extra.append(hira_run.translate(_HIRA_TO_KATA))

    return " ".join(extra)


def tokenize_for_search(text: str) -> str:
    """Tokenize text for BM25 search. Returns space-separated lemmas.

    For Japanese text: uses Sudachi morphological analysis to produce
    dictionary-form tokens (e.g., "走った" → "走る").

    For non-CJK text: returns lowercase text as-is (Qdrant's word
    tokenizer handles English well).

    Args:
        text: Input text to tokenize

    Returns:
        Space-separated lemmatized tokens
    """
    if not text:
        return ""

    if not _CJK_PATTERN.search(text):
        return text.lower()

    return _sudachi_extract(text, lambda t: t.dictionary_form().lower(), "tokenization")
