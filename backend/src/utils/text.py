"""Text processing utilities for search and storage.

Issue #163: Unicode normalization for consistent search across different
character representations (half-width/full-width, composed/decomposed).
Issue #173: Size limits to prevent DoS attacks via large text normalization.
"""

import logging
import os
import unicodedata

logger = logging.getLogger(__name__)

# Issue #173: Maximum text size for normalization (default: 100KB)
# Can be overridden via environment variable MAX_NORMALIZE_SIZE
MAX_NORMALIZE_SIZE = int(os.getenv("MAX_NORMALIZE_SIZE", "102400"))  # 100KB in bytes


def normalize_for_search(text: str | None) -> str | None:
    """Normalize text for search operations.

    Applies NFKC normalization followed by NFC:
    - NFKC: Converts half-width to full-width, compatibility chars to canonical
      - "ｶﾀｶﾅ" (half-width) → "カタカナ" (full-width)
      - "①" → "1", "㈱" → "(株)"
    - NFC: Composes characters (e.g., か + ゛ → が)

    This ensures consistent matching regardless of input encoding:
    - User searches "ｶﾀｶﾅ" (half-width) → matches "カタカナ" (full-width stored)
    - User searches "が" (NFD: 2 codepoints) → matches "が" (NFC: 1 codepoint)

    Issue #173: Size limit protection against DoS attacks.
    Raises ValueError if text exceeds MAX_NORMALIZE_SIZE.

    Args:
        text: Input text (any Unicode), or None

    Returns:
        Normalized text suitable for search, or None if input is None

    Raises:
        ValueError: If text size exceeds MAX_NORMALIZE_SIZE bytes

    Example:
        >>> normalize_for_search("ｶﾀｶﾅ")
        "カタカナ"
        >>> normalize_for_search("㈱会社")
        "(株)会社"
        >>> normalize_for_search(None)
        None
    """
    if text is None:
        return None

    if not text:
        return text

    # Issue #173: Check text size to prevent DoS attacks
    text_size = len(text.encode("utf-8"))
    if text_size > MAX_NORMALIZE_SIZE:
        logger.warning(
            f"Text too large for normalization: {text_size} bytes (max: {MAX_NORMALIZE_SIZE} bytes)"
        )
        raise ValueError(
            f"Text size ({text_size} bytes) exceeds maximum allowed "
            f"({MAX_NORMALIZE_SIZE} bytes) for normalization"
        )

    # NFKC first (compatibility decomposition + canonical composition)
    # This converts: ｶﾀｶﾅ → カタカナ, ① → 1, ㈱ → (株)
    normalized = unicodedata.normalize("NFKC", text)

    # NFC for final composition (ensures consistent representation)
    # This composes: か + ゛ → が (2 codepoints → 1 codepoint)
    normalized = unicodedata.normalize("NFC", normalized)

    return normalized


def detect_symbol_density(text: str, threshold: float = 0.3) -> bool:
    """Detect if text has high symbol density.

    High symbol density suggests the text might be code, URLs, or
    technical content where keyword/exact-match search may be more
    effective than semantic search.

    Args:
        text: Input text
        threshold: Symbol ratio threshold (default: 0.3 = 30%).
                   Must be between 0.0 and 1.0.

    Returns:
        True if symbol density exceeds threshold

    Raises:
        ValueError: If threshold is not between 0.0 and 1.0

    Example:
        >>> detect_symbol_density("https://example.com/path?q=1")
        True
        >>> detect_symbol_density("Hello world")
        False
        >>> detect_symbol_density("C++ node.js AWS_S3")
        True
    """
    # Validate threshold bounds
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"Threshold must be between 0.0 and 1.0, got {threshold}")

    if not text:
        return False

    # Count non-alphanumeric, non-space characters
    symbol_count = sum(1 for c in text if not (c.isalnum() or c.isspace()))
    total_chars = len(text)

    if total_chars == 0:
        return False

    return (symbol_count / total_chars) > threshold
