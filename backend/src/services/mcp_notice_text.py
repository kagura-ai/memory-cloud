"""What a service notice may say (Issue #2762): one pure sanitiser.

A notice is text an administrator of this server wrote and the server places
in an AI client's context, so it is held to plain, visible, single-line prose
that no client renders as a link or as markup. :func:`normalize_notice_text`
is applied when a notice is written and again to every stored row before it
is delivered; a row that fails it is skipped (and logged without its text),
never delivered.

Rules, in order (the first one that fails names the reason):

a. Unicode NFC; U+3000 (ideographic space) becomes U+0020; U+0020 is stripped
   from both ends and runs of it collapse to one.
b. Refused (``control_or_format_char``): every character in the categories
   ``Cc`` (``\\n`` and ``\\t`` included: a notice is one line), ``Cf`` (bidi
   controls, zero-width characters, the BOM), ``Zl``, ``Zp``, ``Co``, ``Cs``,
   ``Cn``, and every ``Zs`` other than U+0020.
c. Refused (``invisible_char``): the Unicode ``Default_Ignorable_Code_Point``
   set (an explicit table below; Python has no property lookup) and U+2800
   (braille blank). U+FE0F is allowed only directly after an emoji base, once;
   U+FE00-FE0E and U+E0100-E01EF are always refused; a run of more than two
   combining marks (``Mn`` / ``Me``) is refused.
d. Refused (``markup_char``): ``< > [ ] ` " \\ { } @`` (HTML, autolinks,
   markdown links, code spans, a forged JSON frame, e-mail addresses), checked
   on the folded copy of rule e so fullwidth forms count too.
e. Refused (``link``), checked on ``NFKC(text).casefold()`` with U+3002, U+FF0E
   and U+FF61 read as ``.``: a ``scheme:`` followed by ``//``; the schemes
   ``mailto javascript data vbscript file tel sms``; ``www.``; host names
   (``label.label`` ending in two or more letters); any ``xn--`` label; IPv4
   and IPv6 literals. There is no exception for this server's own host: a
   notice carries no link at all. Accepted false positives such as
   ``README.md`` or ``Node.js``: the writer rewords.
f. 1 to 300 characters (``empty`` / ``too_long``) and at most 900 UTF-8 bytes
   (``too_many_bytes``) after normalisation.

The stored text is the NFC result of rule a. The reason never carries the
text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Literal

__all__ = [
    "MCP_NOTICE_MAX_BYTES",
    "MCP_NOTICE_MAX_CHARS",
    "NOTICE_TEXT_REASONS",
    "NoticeTextError",
    "NoticeTextReason",
    "normalize_notice_text",
]

MCP_NOTICE_MAX_CHARS: Final = 300
# 300 Japanese characters are 900 bytes in UTF-8.
MCP_NOTICE_MAX_BYTES: Final = 900
# Anything this long is refused before any normalisation work.
_RAW_INPUT_LIMIT: Final = 10_000

NoticeTextReason = Literal[
    "empty",
    "too_long",
    "too_many_bytes",
    "control_or_format_char",
    "invisible_char",
    "markup_char",
    "link",
]
NOTICE_TEXT_REASONS: Final[tuple[NoticeTextReason, ...]] = (
    "empty",
    "too_long",
    "too_many_bytes",
    "control_or_format_char",
    "invisible_char",
    "markup_char",
    "link",
)


class NoticeTextError(ValueError):
    """The text breaks a rule; ``reason`` names which (never the text)."""

    def __init__(self, reason: NoticeTextReason) -> None:
        super().__init__(f"notice text rejected: {reason}")
        self.reason: NoticeTextReason = reason


# --- rule b -------------------------------------------------------------------

_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"})

# --- rule c -------------------------------------------------------------------

# Default_Ignorable_Code_Point, from DerivedCoreProperties.txt of Unicode 15.1
# (unchanged in 16.0). Inclusive ranges. Most of them are Cf or Cn and already
# refused by rule b; the ones in other categories (Mn, Lo) are why the table
# exists. U+FE0F is in it and handled by its own rule.
_DEFAULT_IGNORABLE: Final[tuple[tuple[int, int], ...]] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_BRAILLE_BLANK: Final = 0x2800
_EMOJI_PRESENTATION: Final = 0xFE0F
_MAX_COMBINING_RUN: Final = 2
# Characters that start an emoji presentation sequence besides symbols:
# keycap bases and the few punctuation emoji.
_EMOJI_BASE_EXTRA: Final = frozenset("#*0123456789‼⁉〰〽")

# --- rule d -------------------------------------------------------------------

_MARKUP_CHARS: Final = frozenset('<>[]`"\\{}@')

# --- rule e -------------------------------------------------------------------

_DOTS: Final = str.maketrans({"。": ".", "．": ".", "｡": "."})
_LINK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # scheme followed by // (spaces between tolerated)
    re.compile(r"[a-z][a-z0-9+.-]*: *//", re.ASCII),
    # schemes that need no //
    re.compile(r"\b(?:mailto|javascript|data|vbscript|file|tel|sms):", re.ASCII),
    re.compile(r"www\.", re.ASCII),
    # host-like tokens; ASCII word boundaries so "example.comへ" still ends
    re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b", re.ASCII),
    re.compile(r"xn--", re.ASCII),
    # IPv4
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", re.ASCII),
    # IPv6: the full eight-group form, or any compressed form (it holds "::")
    re.compile(r"\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b", re.ASCII),
    re.compile(r"::"),
)


def _is_default_ignorable(cp: int) -> bool:
    return any(low <= cp <= high for low, high in _DEFAULT_IGNORABLE)


def _is_emoji_base(ch: str) -> bool:
    """An approximation of Emoji: symbols, keycap bases and the punctuation emoji."""
    if ord(ch) == _BRAILLE_BLANK:
        return False
    return unicodedata.category(ch) in ("So", "Sm", "Sk") or ch in _EMOJI_BASE_EXTRA


def _normalise(raw: str) -> str:
    text = unicodedata.normalize("NFC", raw).replace("　", " ")
    return re.sub(" {2,}", " ", text.strip(" "))


def _check_categories(text: str) -> None:
    for ch in text:
        category = unicodedata.category(ch)
        if category in _REFUSED_CATEGORIES or (category == "Zs" and ch != " "):
            raise NoticeTextError("control_or_format_char")


def _check_invisible(text: str) -> None:
    previous = ""
    combining_run = 0
    for ch in text:
        cp = ord(ch)
        if cp == _EMOJI_PRESENTATION:
            if not previous or not _is_emoji_base(previous):
                raise NoticeTextError("invisible_char")
        elif cp == _BRAILLE_BLANK or _is_default_ignorable(cp):
            raise NoticeTextError("invisible_char")
        if unicodedata.category(ch) in ("Mn", "Me"):
            combining_run += 1
            if combining_run > _MAX_COMBINING_RUN:
                raise NoticeTextError("invisible_char")
        else:
            combining_run = 0
        previous = ch


def _folded(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().translate(_DOTS)


def normalize_notice_text(raw: str) -> str:
    """The stored form of a notice text, or :class:`NoticeTextError`.

    Pure and idempotent: ``normalize_notice_text(normalize_notice_text(x))``
    returns its argument. See the module docstring for the rules.

    Raises:
        NoticeTextError: ``reason`` is one of :data:`NOTICE_TEXT_REASONS`.
    """
    if len(raw) > _RAW_INPUT_LIMIT:
        raise NoticeTextError("too_long")
    text = _normalise(raw)
    _check_categories(text)
    _check_invisible(text)
    folded = _folded(text)
    if any(ch in _MARKUP_CHARS for ch in folded):
        raise NoticeTextError("markup_char")
    if any(pattern.search(folded) for pattern in _LINK_PATTERNS):
        raise NoticeTextError("link")
    if not text:
        raise NoticeTextError("empty")
    if len(text) > MCP_NOTICE_MAX_CHARS:
        raise NoticeTextError("too_long")
    if len(text.encode("utf-8")) > MCP_NOTICE_MAX_BYTES:
        raise NoticeTextError("too_many_bytes")
    return text
