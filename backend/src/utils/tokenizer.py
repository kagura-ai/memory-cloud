"""Japanese-aware tokenizer for BM25 search.

Uses Sudachi for morphological analysis with lemmatization.
Falls back to simple word splitting for non-CJK text.
"""

import re

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


# Hiragana → Katakana translation table (for query normalization)
_HIRA_TO_KATA = str.maketrans(
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ"
    "まみむめもやゆよらりるれろわをんがぎぐげござじずぜぞだぢづでど"
    "ばびぶべぼぱぴぷぺぽぁぃぅぇぉっゃゅょー",
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホ"
    "マミムメモヤユヨラリルレロワヲンガギグゲゴザジズゼゾダヂヅデド"
    "バビブベボパピプペポァィゥェォッャュョー",
)


def text_to_reading(text: str) -> str:
    """Convert text to space-separated katakana readings using Sudachi.

    Issue #73: For BM25 matching of full-hiragana queries (voice input, IME).
    Returns token-level readings with stop words removed, matching the
    tokenize_for_search() filtering for consistent BM25 matching.

    Args:
        text: Input text (any script)

    Returns:
        Space-separated katakana reading tokens
    """
    if not text:
        return ""

    try:
        tokenizer = _get_sudachi()
        tokens = tokenizer.tokenize(text)
        readings = []
        for token in tokens:
            pos = token.part_of_speech()[0]
            if pos in _STOP_POS:
                continue
            readings.append(token.reading_form())
        return " ".join(readings)
    except Exception as e:
        logger.warning("sudachi_reading_failed", text_length=len(text), error=str(e))
        return ""


def hiragana_to_katakana(text: str) -> str:
    """Convert hiragana to katakana for query normalization.

    Args:
        text: Text possibly containing hiragana

    Returns:
        Text with hiragana converted to katakana
    """
    return text.translate(_HIRA_TO_KATA)


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

    # If no CJK characters, return as-is (let Qdrant handle English)
    if not _CJK_PATTERN.search(text):
        return text.lower()

    try:
        tokenizer = _get_sudachi()
        tokens = tokenizer.tokenize(text)
        lemmas = []
        for token in tokens:
            pos = token.part_of_speech()[0]
            if pos in _STOP_POS:
                continue
            lemmas.append(token.dictionary_form().lower())
        return " ".join(lemmas)
    except Exception as e:
        logger.warning(
            "sudachi_tokenization_failed", text_length=len(text), error=str(e), exc_info=True
        )
        return text.lower()
