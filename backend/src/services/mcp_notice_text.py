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
Rules d and e read a *skeleton* of the text: ``NFKC(text).casefold()`` with
every combining mark removed (NFKD, then every ``M*`` character dropped), so
fullwidth forms count and a mark inside a token (U+0332 after ``https``, the
``é`` of ``loginé.com``) does not split it. The stored text keeps its marks.

d. Refused (``markup_char``): ``< > [ ] ` " \\ { } @`` (HTML, autolinks,
   markdown links, code spans, a forged JSON frame, e-mail addresses), and
   HTML character references, which markdown renderers decode (``&#`` with a
   digit or ``x``, ``&name;``, and ``&lt`` ``&gt`` ``&quot`` without the
   semicolon). A plain ``&`` (``Q&A``) passes.
e. Refused (``link``), with U+FF0E read as ``.`` (NFKC maps it), and U+3002
   and U+FF61 read as ``.`` unless the character directly before or directly
   after it is Japanese (Han, Hiragana, Katakana with the prolonged sound
   mark, CJK symbols and punctuation, fullwidth and halfwidth forms): between
   two other characters a browser reads it as a dot (``example。com``,
   ``examplе。com`` with a Cyrillic ``е``, ``test_。com``, ``пример。рф``);
   next to a Japanese character it ends a sentence (「…しました。Kagura」,
   「対象は MCP。保存は…」, 「接続先は MCP。」): a ``scheme:`` followed by ``//``;
   ``//`` directly followed by anything but a space (a protocol-relative
   link such as ``//localhost``, whatever the host); the schemes ``mailto javascript data vbscript file tel sms``;
   ``www.``; host names (``label.label`` ending in two or more ASCII letters,
   the labels in any script: ``例え.jp``, ``пример.com``); a label in any
   script followed by a dot (as read above) and a top-level label of two or
   more letters in any script (``пример.рф``, ``пример。рф``, ``例え.みんな``);
   any ``xn--`` label; IPv4 and IPv6
   literals. There is no exception for this server's own host: a notice
   carries no link at all. Accepted false positives such as ``README.md``,
   ``Node.js``, ``Ver.アップ``, ``a//b`` or ``。`` with ASCII on both sides
   (``Kagura。Kagura``, ``MCP。Kagura``): the writer rewords or puts a space
   after ``。``. Known residuals, accepted: a label whose character next to
   ``。`` is Japanese (``例え。jp``, ``exa例。com``) is read as prose. Such a
   host always keeps a non-ASCII character, but a browser still resolves it,
   and a few Han or kana characters resemble Latin ones; the ideographic
   numerals ``〇`` and ``〡`` do not count as Japanese here, so
   ``hell〇。com`` is refused.
f. 1 to 300 characters (``empty`` / ``too_long``) and at most 900 UTF-8 bytes
   (``too_many_bytes``) after normalisation.

The stored text is the NFC result of rule a. The reason never carries the
text.

Cost: a raw input longer than 1,200 characters is refused (``too_long``)
before any work. Every pattern below is linear in the folded text: none has a
repeated group or an unbounded run that is retried from every position of
the run (runs start only at the beginning of a run and are possessive), so a
pathological input at the bound, NFKC expansion included (one character can
become 18), costs a few milliseconds; ``tests/services/test_mcp_notice_text.py``
measures it.
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
# Anything this long is refused (too_long) before any normalisation work:
# four times the 300-character cap leaves room for spaces that collapse and
# marks that compose, and keeps one request's CPU in the milliseconds.
_RAW_INPUT_LIMIT: Final = 1_200

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
# HTML character references: numeric (decoded even without the semicolon),
# named, and the legacy names an HTML parser decodes without one.
_CHARACTER_REFERENCE: Final = re.compile(
    r"&(?:#(?:\d|x[0-9a-f])|[a-z][a-z0-9]*;|lt|gt|quot)", re.ASCII
)

# --- rule e -------------------------------------------------------------------

# Characters that make an adjacent U+3002 the end of a Japanese sentence:
# Han (radicals, strokes, unified ideographs and their extensions,
# compatibility ideographs), Hiragana, Katakana (with its phonetic extensions,
# the prolonged sound mark U+30FC and the kana supplements), CJK symbols and
# punctuation U+3000-303F (brackets, 々, 〆), the CJK vertical and
# compatibility forms, and the fullwidth / halfwidth forms U+FF00-FFEF (NFKC
# maps most of them, halfwidth katakana included, to the ranges above; the
# range is kept for any that survive). Hangul is not in it, and neither are
# the ideographic numerals U+3007 and U+3021-3029 / U+3038-303A (〇 and 〡
# look like Latin letters), so ``hell〇。com`` stays a host name.
_JAPANESE_NEIGHBOUR: Final = (
    "\u2e80-\u2fdf"
    "\u3000-\u3006"
    "\u3008-\u3020"
    "\u302a-\u3037"
    "\u303b-\u30ff"
    "\u31c0-\u31ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\ufe10-\ufe1f"
    "\ufe30-\ufe4f"
    "\uff00-\uffef"
    "\U0001b000-\U0001b16f"
    "\U00020000-\U0003ffff"
)
# U+3002 (and U+FF61, which NFKC maps to it) read as a dot unless the
# character directly before OR directly after it is Japanese: only then is
# it the end of a sentence; between two other characters (ASCII, Cyrillic
# look-alikes, an underscore, start or end of the text) a browser may read
# the token as a host name. Symmetric, so the last character of a label
# cannot carry a look-alike host through. One character of look-behind and
# look-ahead each: linear. U+FF0E is a dot already (NFKC maps it to U+002E).
_NON_JAPANESE_FULL_STOP: Final = re.compile(
    f"(?<![{_JAPANESE_NEIGHBOUR}])[。｡](?![{_JAPANESE_NEIGHBOUR}])"
)
# Every pattern is linear: an unbounded run is only entered at the start of
# the run (a lookbehind) and is possessive, so it is never rescanned from each
# of its positions; bounded runs ({1,4}, {2,63}) cost a constant per position.
_LINK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # scheme followed by // (spaces between tolerated): the run of scheme
    # characters before ":" holds a letter (where a scheme can start)
    re.compile(r"(?<![a-z0-9+.-])[0-9+.-]*+[a-z][a-z0-9+.-]*+: *//", re.ASCII),
    # a protocol-relative start: "//" directly followed by anything but a
    # space ("//localhost", "//a.b", "//例え"); renderers link these whatever
    # the host, so no host rule below has to recognise it (rule a leaves
    # U+0020 as the only space)
    re.compile(r"//[^ ]"),
    # schemes that need no //
    re.compile(r"\b(?:mailto|javascript|data|vbscript|file|tel|sms):", re.ASCII),
    re.compile(r"www\.", re.ASCII),
    # host-like tokens ``(?:label\.)+tld``; ASCII word boundaries so
    # "example.comへ" still ends. Any such match ends in one label character
    # right before the last dot, so that is all the pattern needs to find (no
    # repeated group to backtrack through).
    re.compile(r"[a-z0-9]\.[a-z]{2,63}\b", re.ASCII),
    # the same with labels in any script before an ASCII top-level label: the
    # run of word characters and hyphens before the dot holds a letter or digit
    re.compile(r"(?<![\w-])[_-]*+[^\W_][\w-]*+\.[a-z]{2,63}(?![a-z0-9])"),
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


# A top-level label in any script after a ``.``: U+002E, the fullwidth
# U+FF0E (NFKC maps it), or a ``。`` between two non-Japanese characters
# (``пример。рф``). A ``。`` with a Japanese neighbour stays ``。``, so
# ordinary prose (「確認しました。次へ」, 「対象は MCP。次へ」) is not read as a
# host. Linear like the patterns above.
_ANY_SCRIPT_HOST: Final = re.compile(r"(?<![\w-])[_-]*+[^\W_][\w-]*+\.[^\W\d_]{2,63}")


def _skeleton(text: str) -> str:
    """``NFKC(text).casefold()`` with every combining mark removed."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.category(ch).startswith("M"))


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
    skeleton = _skeleton(text)
    if any(ch in _MARKUP_CHARS for ch in skeleton) or _CHARACTER_REFERENCE.search(skeleton):
        raise NoticeTextError("markup_char")
    folded = _NON_JAPANESE_FULL_STOP.sub(".", skeleton)
    if any(pattern.search(folded) for pattern in _LINK_PATTERNS) or _ANY_SCRIPT_HOST.search(folded):
        raise NoticeTextError("link")
    if not text:
        raise NoticeTextError("empty")
    if len(text) > MCP_NOTICE_MAX_CHARS:
        raise NoticeTextError("too_long")
    if len(text.encode("utf-8")) > MCP_NOTICE_MAX_BYTES:
        raise NoticeTextError("too_many_bytes")
    return text
