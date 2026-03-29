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
            form = token.dictionary_form()
            if len(form) >= 2 or not form.isascii():
                lemmas.append(form.lower())
        return " ".join(lemmas)
    except Exception:
        logger.warning("sudachi_tokenization_failed", text_length=len(text))
        return text.lower()
