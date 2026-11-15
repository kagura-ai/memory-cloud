"""The ``search`` parameter of an admin list: one normalisation, one LIKE pattern.

A list route declares the parameter as ``Annotated[str | None,
AfterValidator(search_validator(<column width>))]`` and passes the validated
value to ``substring_pattern``; the column is matched with
``column.ilike(pattern, escape=LIKE_ESCAPE)``.

The text is what a person typed to find a row (a name, a label, an address),
so it is never written to a log line: log that one was given and its length.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

LIKE_ESCAPE = "\\"


def normalize_search(value: str | None, max_length: int) -> str | None:
    """Trim a search text; empty means no filter.

    Args:
        value: The raw query parameter.
        max_length: The most characters the text may have (the searched
            column's width: a longer text cannot be a substring of it).

    Returns:
        The trimmed text, or ``None`` when nothing is left.

    Raises:
        ValueError: The text is longer than ``max_length`` or, once trimmed,
            holds a control character. The message never repeats the text.
    """
    if value is not None and len(value) > max_length:
        raise ValueError(f"must have at most {max_length} characters")
    normalized = (value or "").strip()
    if not normalized:
        return None
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise ValueError("must not contain control characters")
    return normalized


def search_validator(max_length: int) -> Callable[[str | None], str | None]:
    """``normalize_search`` bound to a length, for a pydantic ``AfterValidator``."""

    def _validate(value: str | None) -> str | None:
        return normalize_search(value, max_length)

    return _validate


def substring_pattern(normalized: str) -> str:
    """The LIKE pattern that matches ``normalized`` as a literal substring.

    ``\\``, ``%`` and ``_`` are escaped (the backslash first), so they match
    as characters. Use with ``escape=LIKE_ESCAPE``.

    Args:
        normalized: A text ``normalize_search`` returned.

    Returns:
        ``%<escaped text>%``.
    """
    escaped = (
        normalized.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"
