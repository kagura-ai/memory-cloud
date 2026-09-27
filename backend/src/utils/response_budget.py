"""Response-size budgets for MCP tool results (#1743).

#1685 bounded ``reference()``. The same rule now covers every tool whose reply
grows with stored data: a default call stays well under what an MCP client
passes to the model (Claude Code: 25k tokens; claude.ai: about 150k
characters), and a call at the largest allowed parameters either fits or says
it was cut (``truncated`` / ``has_more`` / ``<field>_omitted``).

The unit is CHARACTERS of the compact JSON the model reads (the ``_dumps``
form: ``ensure_ascii=False``, no spaces) — the measure #1685 chose. The
defaults reuse its numbers: 20,000 characters keeps an English reply near 5k
tokens and a Japanese one under the 25k-token cap.

This module is shared by the MCP handlers and by services that build a whole
MCP envelope (``AgentBootstrapService``), so it lives outside the tools
package and has no MCP dependency.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

# Same numbers as #1685's reference() budget (mcp_server/tools/_constants.py).
DEFAULT_MAX_CHARS = 20_000
MIN_MAX_CHARS = 10_000
MAX_CHARS_LIMIT = 100_000


class BudgetArgumentError(ValueError):
    """A ``max_chars`` / ``limit`` / ``cursor`` argument that is not valid."""

    def __init__(self, message: str, received: Any) -> None:
        super().__init__(message)
        self.message = message
        self.received = received


def json_chars(obj: Any) -> int:
    """Characters of ``obj`` as compact UTF-8 JSON (the tool-result form)."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def parse_max_chars(raw: Any, *, default: int = DEFAULT_MAX_CHARS) -> int:
    """Validate an optional ``max_chars`` argument (#1685 convention).

    ``type(x) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``.

    Raises:
        BudgetArgumentError: not an integer in [MIN_MAX_CHARS, MAX_CHARS_LIMIT].
    """
    if raw is None:
        return default
    if type(raw) is not int or not (MIN_MAX_CHARS <= raw <= MAX_CHARS_LIMIT):
        raise BudgetArgumentError(
            f"max_chars must be an integer between {MIN_MAX_CHARS} and "
            f"{MAX_CHARS_LIMIT} (characters, not tokens).",
            raw,
        )
    return raw


def parse_limit(raw: Any, *, default: int, maximum: int, name: str = "limit") -> int:
    """Validate an optional page size, clamping it into ``[1, maximum]``.

    A non-integer is an error; an integer out of range is clamped, so a caller
    that asked for more than the page maximum still gets a page (with
    ``has_more``) instead of a refusal.

    Raises:
        BudgetArgumentError: not an integer.
    """
    if raw is None:
        return default
    if type(raw) is not int:
        raise BudgetArgumentError(f"{name} must be an integer.", raw)
    return max(1, min(raw, maximum))


def parse_offset_cursor(raw: Any, name: str = "cursor") -> int:
    """Decode an offset cursor (the ``next_cursor`` of a previous page).

    Raises:
        BudgetArgumentError: not a string holding a non-negative integer.
    """
    if raw is None or raw == "":
        return 0
    if not isinstance(raw, str) or not raw.isdigit():
        raise BudgetArgumentError(
            f"{name} must be the next_cursor of a previous response.",
            raw,
        )
    return int(raw)


def fit_items(items: Sequence[Any], budget: int) -> int:
    """How many leading ``items`` fit ``budget`` characters as JSON array members.

    Each member costs its compact JSON plus one separator. Order is kept, so
    the result is a prefix: the caller reports the rest as truncated / pages
    on from it.
    """
    used = 0
    for index, item in enumerate(items):
        used += json_chars(item) + 1
        if used > budget:
            return index
    return len(items)


def drop_key(items: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Copies of ``items`` without ``key`` (the first thing cut under a budget)."""
    return [{k: v for k, v in item.items() if k != key} for item in items]
