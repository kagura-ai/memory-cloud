"""``details.tool_trigger`` contract — the write-side validator for tool guardrails.

A tool guardrail is a memory that a client-side hook injects into the model's
context at the moment a matching tool call happens. The memory carries::

    details.tool_trigger = {
        "tool":   "Bash|PowerShell",   # required regex, full match on the tool name
        "on":     "pre",               # "pre" (default) | "result"
        "match":  "gh pr merge\\b.*--delete-branch",  # optional regex, searched in the subject
        "action": "inform",            # "inform" (default) | "block"
    }

The server's whole job is to make sure that what it hands to hooks is safe
to run in a backtracking regex engine, in Python and in JavaScript alike.
It therefore validates every pattern with a hand-written recursive-descent
parser over an explicit ALLOWLIST grammar (the JS ∩ Python subset), and
rejects everything the grammar does not enumerate — backreferences,
lookaround, named/atomic groups, possessive quantifiers, inline flags other
than a leading ``(?i)``, unknown escapes, bounds above ``QUANTIFIER_MAX``, a
quantifier on a group that itself contains a quantifier or ``|``, and two
unbounded quantifiers with nothing mandatory between them (``\\w+\\s*\\w+``
is polynomial in every backtracking engine even though no quantifier is
nested). ``sre_parse`` is deliberately not used: it accepts Python-only
syntax we would have to walk and reject anyway, and it is private API.

After the grammar passes, ``re.compile(pattern)`` runs once as a belt-and-
braces check. **No pattern is ever evaluated against an input on the
server** — no ``.search`` / ``.match`` / ``.fullmatch`` / ``.sub`` — the
read lane returns the pattern as data and matching happens in the client
hook. ``tests/utils/test_tool_trigger_regex.py`` pins this with a
``re.compile`` spy (so this module must call ``re.compile`` through the
``re`` module object, never ``from re import compile``) and an ``ast`` scan.

Rejections raise :class:`ToolTriggerValidationError` whose message is
``"<code>: <sentence>"``. The code is a stable token clients and tests match
on (``TOOL_TRIGGER_ERROR_CODES`` is the single list); the sentence may change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

# Length caps: ``tool`` is a tool-name matcher, ``match`` a command/path/JSON
# matcher. Both are also bounded by the 8 KB subject cap clients apply.
TOOL_PATTERN_MAX_CHARS = 128
MATCH_PATTERN_MAX_CHARS = 200
# ``{n,m}`` upper bound. Bounded repeats up to 100 measured harmless even when
# adjacent and overlapping (``\w{1,100}\s{0,100}\w{1,100}=`` on 2 KB: 0.04 s).
QUANTIFIER_MAX = 100
# Unbounded quantifiers (``*``, ``+``, ``{n,}``) per pattern. Even separated
# by mandatory atoms each one is a backtracking point; four is plenty for a
# tool-call matcher and keeps the worst case polynomial of small degree.
MAX_UNBOUNDED_QUANTIFIERS = 4
# Group nesting depth — bounds the recursive-descent parser itself.
MAX_GROUP_DEPTH = 8

TOOL_TRIGGER_ON: tuple[str, ...] = ("pre", "result")
TOOL_TRIGGER_ACTIONS: tuple[str, ...] = ("inform", "block")
TOOL_TRIGGER_DEFAULT_ON = "pre"
TOOL_TRIGGER_DEFAULT_ACTION = "inform"

# The shared cache / payload format version (docs/mcp-tools.md § Tool
# guardrails). Additive fields never bump it; a changed or removed field does.
GUARDRAIL_FORMAT = 1

_ALLOWED_KEYS = frozenset({"tool", "on", "match", "action"})

# code -> what it means. Every code the module raises is listed here (pinned
# by the test suite) so docs, tests and clients share one vocabulary.
TOOL_TRIGGER_ERROR_CODES: dict[str, str] = {
    # structural
    "tool_trigger_not_object": "details.tool_trigger must be a JSON object",
    "tool_trigger_unknown_key": "details.tool_trigger has a key outside {tool, on, match, action}",
    "tool_required": "tool_trigger.tool is required and must be non-empty",
    "tool_not_string": "tool_trigger.tool must be a string",
    "match_not_string": "tool_trigger.match must be a string when present",
    "match_empty": "tool_trigger.match must not be empty (it would match everything)",
    "pattern_too_long": "tool_trigger.tool is limited to 128 characters, match to 200",
    "on_invalid": "tool_trigger.on must be 'pre' or 'result'",
    "action_invalid": "tool_trigger.action must be 'inform' or 'block'",
    "block_requires_pre": "action='block' is only allowed with on='pre'",
    "block_requires_match": "action='block' requires a match pattern",
    "block_match_not_specific": "action='block' requires a match with at least one literal",
    "tool_trigger_requires_user_credential": (
        "tool_trigger can only be written with a user credential, not an agent credential"
    ),
    "pattern_control_char": "patterns may not contain control characters (U+0000-U+001F)",
    # grammar
    "regex_backreference": "backreferences are not allowed",
    "regex_lookaround": "lookahead / lookbehind is not allowed",
    "regex_named_group": "named groups are not allowed",
    "regex_possessive_or_atomic": "atomic groups and possessive quantifiers are not allowed",
    "regex_inline_flag": "inline flags are not allowed except a single leading (?i)",
    "regex_unknown_escape": "escape sequence outside the allowed set",
    "regex_bound_too_large": f"a repetition bound may not exceed {QUANTIFIER_MAX}",
    "regex_bound_inverted": "a repetition {n,m} needs n <= m",
    "regex_nested_quantifier": (
        "a quantifier may not apply to a group that itself contains a quantifier or '|'"
    ),
    "regex_stacked_quantifier": "a quantifier may not follow another quantifier",
    "regex_dangling_quantifier": "a quantifier needs something to repeat",
    "regex_adjacent_unbounded": (
        "two unbounded quantifiers (*, +, {n,}) need a mandatory atom between them"
    ),
    "regex_too_many_unbounded": (
        f"at most {MAX_UNBOUNDED_QUANTIFIERS} unbounded quantifiers per pattern"
    ),
    "regex_nesting_too_deep": f"groups may nest at most {MAX_GROUP_DEPTH} levels",
    "regex_class_unsupported": "nested classes and set operations are not allowed in [...]",
    "regex_class_empty": "an empty character class is not allowed",
    "regex_empty_group": "an empty group is not allowed",
    "regex_syntax": "the pattern is not valid in the allowed subset",
}


class ToolTriggerValidationError(ValueError):
    """A caller-supplied ``details.tool_trigger`` is invalid.

    Subclasses ``ValueError`` so the existing MCP ``validation_error`` / REST
    422 mappings (the ``TriggerValidationError`` / ``LocationValidationError``
    wiring) apply as-is. ``code`` is the stable token; ``str(exc)`` is
    ``"<code>: <sentence>"``.
    """

    def __init__(self, code: str, sentence: str) -> None:
        if code not in TOOL_TRIGGER_ERROR_CODES:  # pragma: no cover — programmer error
            raise KeyError(code)
        super().__init__(f"{code}: {sentence}")
        self.code = code


# --------------------------------------------------------------------------- #
# Safe-regex grammar
# --------------------------------------------------------------------------- #

# Escapes that stand for one literal character (JS and Python agree).
_LITERAL_ESCAPES = frozenset("\\.*+?()[]{}|^$/-") | frozenset("ntrfv")
# Class escapes — one atom, never a literal (they do not satisfy
# ``block_match_not_specific``).
_CLASS_ESCAPES = frozenset("dDwWsS")
_ZERO_WIDTH_ESCAPES = frozenset("bB")
_HEX = frozenset("0123456789abcdefABCDEF")
_QUANTIFIER_STARTS = frozenset("*+?{")
_CLASS_SET_OPS = ("&&", "--", "~~", "||")


@dataclass
class _Atom:
    """What the sequence walker needs to know about one parsed element."""

    kind: str  # "atom" | "zero_width" | "group"
    literal: bool = False
    # group-only: how the body behaves at its edges (see _Seq)
    starts_unbounded: bool = False
    ends_unbounded: bool = False
    mandatory: bool = False
    has_quantifier: bool = False
    has_alternation: bool = False


@dataclass
class _Seq:
    """Summary of one alternative (a ``sequence``) or a whole group body.

    ``starts_unbounded`` — before any mandatory atom, an unbounded one occurs.
    ``ends_unbounded`` — an unbounded run is still open at the end.
    ``mandatory`` — at least one atom must consume input (so the sequence can
    separate two unbounded runs around it).
    """

    starts_unbounded: bool = False
    ends_unbounded: bool = False
    mandatory: bool = False
    has_quantifier: bool = False
    has_alternation: bool = False
    literals: int = 0


class _Parser:
    """Recursive-descent parser for the allowlist grammar.

    ``pattern     := [ "(?i)" ] alternation``
    ``alternation := sequence ( "|" sequence )*``
    ``sequence    := ( atom quantifier? )+``
    ``atom        := literal | "." | "^" | "$" | escape | class | group``
    ``group       := "(" alternation ")" | "(?:" alternation ")"``
    ``quantifier  := ( "*" | "+" | "?" | "{n}" | "{n,}" | "{n,m}" ) "?"?``
    """

    def __init__(self, pattern: str, field: str) -> None:
        self.p = pattern
        self.n = len(pattern)
        self.i = 0
        self.field = field
        self.unbounded_count = 0

    # -- helpers ---------------------------------------------------------

    def _fail(self, code: str, sentence: str) -> ToolTriggerValidationError:
        return ToolTriggerValidationError(code, f"{self.field}: {sentence} (at offset {self.i})")

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.p[j] if j < self.n else ""

    # -- entry -----------------------------------------------------------

    def parse(self) -> _Seq:
        if self.p.startswith("(?i)"):
            self.i = 4
        body = self._alternation(depth=0)
        if self.i < self.n:
            # Only a stray ')' can stop the top-level alternation early.
            raise self._fail("regex_syntax", "unbalanced ')'")
        return body

    # -- alternation / sequence -----------------------------------------

    def _alternation(self, depth: int) -> _Seq:
        branches = [self._sequence(depth)]
        while self._peek() == "|":
            self.i += 1
            branches.append(self._sequence(depth))
        merged = _Seq(
            starts_unbounded=any(b.starts_unbounded for b in branches),
            ends_unbounded=any(b.ends_unbounded for b in branches),
            mandatory=all(b.mandatory for b in branches),
            has_quantifier=any(b.has_quantifier for b in branches),
            has_alternation=len(branches) > 1 or any(b.has_alternation for b in branches),
            literals=sum(b.literals for b in branches),
        )
        return merged

    def _sequence(self, depth: int) -> _Seq:
        seq = _Seq()
        pending_unbounded = False
        seen_mandatory = False
        count = 0
        while self.i < self.n and self._peek() not in "|)":
            if self._peek() in _QUANTIFIER_STARTS:
                if self._peek() == "{" and not self._looks_like_bounds():
                    raise self._fail("regex_syntax", "'{' must start a bounded repetition {n,m}")
                raise self._fail(
                    "regex_dangling_quantifier", "a quantifier needs an atom to repeat"
                )
            atom = self._atom(depth)
            count += 1
            quant = self._quantifier()  # None | ("unbounded"|"nullable"|"mandatory")
            if atom.kind == "group":
                seq.has_quantifier = seq.has_quantifier or atom.has_quantifier
                seq.has_alternation = seq.has_alternation or atom.has_alternation
            if quant is not None:
                seq.has_quantifier = True
                if atom.kind == "zero_width":
                    raise self._fail(
                        "regex_dangling_quantifier", "a quantifier may not follow ^ $ \\b \\B"
                    )
                if atom.kind == "group" and (atom.has_quantifier or atom.has_alternation):
                    raise self._fail(
                        "regex_nested_quantifier",
                        "a quantifier may not apply to a group that itself contains a "
                        "quantifier or '|'",
                    )
            if atom.literal:
                seq.literals += 1

            if atom.kind == "zero_width":
                continue

            # A quantified group has a flat body (the nested rule above), so it
            # behaves as one atom of its quantifier's class.
            if atom.kind == "group" and quant is None:
                if atom.starts_unbounded and pending_unbounded:
                    raise self._fail(
                        "regex_adjacent_unbounded",
                        "two unbounded quantifiers need a mandatory atom between them",
                    )
                if atom.starts_unbounded and not seen_mandatory:
                    seq.starts_unbounded = True
                if atom.mandatory:
                    seen_mandatory = True
                    pending_unbounded = atom.ends_unbounded
                elif atom.ends_unbounded:
                    pending_unbounded = True
                continue

            if quant == "unbounded":
                if pending_unbounded:
                    raise self._fail(
                        "regex_adjacent_unbounded",
                        "two unbounded quantifiers need a mandatory atom between them",
                    )
                self.unbounded_count += 1
                if self.unbounded_count > MAX_UNBOUNDED_QUANTIFIERS:
                    raise self._fail(
                        "regex_too_many_unbounded",
                        f"at most {MAX_UNBOUNDED_QUANTIFIERS} unbounded quantifiers per pattern",
                    )
                pending_unbounded = True
                if not seen_mandatory:
                    seq.starts_unbounded = True
            elif quant == "nullable":
                pass  # ?, {0,m}: consumes nothing for sure — does not separate
            else:  # None or "mandatory"
                seen_mandatory = True
                pending_unbounded = False

        if count == 0:
            raise self._fail("regex_syntax", "empty pattern or empty alternative")
        seq.ends_unbounded = pending_unbounded
        seq.mandatory = seen_mandatory
        return seq

    # -- atoms -----------------------------------------------------------

    def _atom(self, depth: int) -> _Atom:
        c = self._peek()
        if c == "(":
            return self._group(depth)
        if c == "[":
            return self._class()
        if c == "\\":
            return self._escape(in_class=False)[0]
        if c in "^$":
            self.i += 1
            return _Atom(kind="zero_width")
        if c == ".":
            self.i += 1
            return _Atom(kind="atom")
        if c == ")":
            raise self._fail("regex_syntax", "unbalanced ')'")
        if c == "]":
            raise self._fail("regex_syntax", "unbalanced ']'")
        if c == "}":
            raise self._fail("regex_syntax", "unbalanced '}'")
        self.i += 1
        return _Atom(kind="atom", literal=True)

    def _group(self, depth: int) -> _Atom:
        depth += 1
        if depth > MAX_GROUP_DEPTH:
            raise self._fail(
                "regex_nesting_too_deep", f"groups may nest at most {MAX_GROUP_DEPTH} levels"
            )
        self.i += 1  # '('
        if self._peek() == "?":
            nxt = self._peek(1)
            if nxt == ":":
                self.i += 2
            elif nxt in ("=", "!") or (nxt == "<" and self._peek(2) in ("=", "!")):
                raise self._fail("regex_lookaround", "lookahead / lookbehind is not allowed")
            elif nxt == "P" or nxt == "<":
                raise self._fail("regex_named_group", "named groups are not allowed")
            elif nxt == ">":
                raise self._fail("regex_possessive_or_atomic", "atomic groups are not allowed")
            else:
                raise self._fail(
                    "regex_inline_flag", "inline flags are only allowed as a single leading (?i)"
                )
        if self._peek() == ")":
            raise self._fail("regex_empty_group", "an empty group is not allowed")
        body = self._alternation(depth)
        if self._peek() != ")":
            raise self._fail("regex_syntax", "unbalanced '('")
        self.i += 1
        return _Atom(
            kind="group",
            literal=body.literals > 0,
            starts_unbounded=body.starts_unbounded,
            ends_unbounded=body.ends_unbounded,
            mandatory=body.mandatory,
            has_quantifier=body.has_quantifier,
            has_alternation=body.has_alternation,
        )

    def _class(self) -> _Atom:
        self.i += 1  # '['
        negated = False
        if self._peek() == "^":
            self.i += 1
            negated = True
        if self._peek() == "]":
            raise self._fail("regex_class_empty", "an empty character class is not allowed")
        literal = False
        first = True
        while True:
            c = self._peek()
            if c == "":
                raise self._fail("regex_syntax", "unbalanced '['")
            if c == "]":
                self.i += 1
                break
            if c == "[":
                raise self._fail(
                    "regex_class_unsupported", "nested classes are not allowed in [...]"
                )
            if self.p.startswith(_CLASS_SET_OPS, self.i):
                raise self._fail(
                    "regex_class_unsupported", "set operations (&& -- ~~ ||) are not allowed"
                )
            lo = self._class_item()
            if lo is not None:
                literal = True
            if self.p.startswith(_CLASS_SET_OPS, self.i):
                raise self._fail(
                    "regex_class_unsupported", "set operations (&& -- ~~ ||) are not allowed"
                )
            # Range: 'x-y' where y is not ']' — a trailing or leading '-' is literal.
            if self._peek() == "-" and self._peek(1) not in ("]", ""):
                if lo is None or (first and c == "-"):
                    raise self._fail("regex_syntax", "a range needs a literal on both ends")
                self.i += 1
                hi = self._class_item()
                if hi is None:
                    raise self._fail("regex_syntax", "a range needs a literal on both ends")
                if ord(lo) > ord(hi):
                    raise self._fail("regex_syntax", "inverted range in [...]")
            first = False
        # A negated class names what NOT to match; it does not make a block
        # pattern specific (``[^x]*`` matches almost everything).
        return _Atom(kind="atom", literal=literal and not negated)

    def _class_item(self) -> str | None:
        """One class member; returns the literal character or None for a class escape."""
        c = self._peek()
        if c == "\\":
            _, resolved = self._escape(in_class=True)
            return resolved
        self.i += 1
        return c

    def _escape(self, *, in_class: bool) -> tuple[_Atom, str | None]:
        """Parse one escape → ``(atom, literal_char)``; ``literal_char`` is None
        for class escapes (``\\d`` …) and zero-width ones."""
        start = self.i
        self.i += 1  # backslash
        c = self._peek()
        if c == "":
            raise self._fail("regex_syntax", "trailing backslash")
        if c == "0":
            raise self._fail("regex_unknown_escape", "octal escapes are not allowed")
        if c.isdigit() or c == "k":
            raise self._fail("regex_backreference", "backreferences are not allowed")
        self.i += 1
        if c in _CLASS_ESCAPES:
            return _Atom(kind="atom"), None
        if c in _ZERO_WIDTH_ESCAPES:
            if in_class:
                self.i = start
                raise self._fail("regex_unknown_escape", "\\b and \\B are not allowed in [...]")
            return _Atom(kind="zero_width"), None
        if c in _LITERAL_ESCAPES:
            resolved = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}.get(c, c)
            return _Atom(kind="atom", literal=True), resolved
        if c in ("x", "u"):
            width = 2 if c == "x" else 4
            digits = self.p[self.i : self.i + width]
            if len(digits) != width or not all(d in _HEX for d in digits):
                self.i = start
                raise self._fail("regex_unknown_escape", f"\\{c} needs exactly {width} hex digits")
            self.i += width
            point = int(digits, 16)
            if point < 0x20:
                self.i = start
                raise self._fail(
                    "pattern_control_char", "control characters are not allowed, even escaped"
                )
            if 0xD800 <= point <= 0xDFFF:
                self.i = start
                raise self._fail("regex_unknown_escape", "lone surrogates are not allowed")
            return _Atom(kind="atom", literal=True), chr(point)
        self.i = start
        raise self._fail("regex_unknown_escape", f"\\{c} is outside the allowed escapes")

    # -- quantifiers -----------------------------------------------------

    def _looks_like_bounds(self) -> bool:
        inner = self._bounds_text()
        if inner is None:
            return False
        lo, sep, hi = inner.partition(",")
        return lo.isdigit() and (hi.isdigit() or hi == "")

    def _bounds_text(self) -> str | None:
        close = self.p.find("}", self.i)
        return None if close == -1 else self.p[self.i + 1 : close]

    def _quantifier(self) -> str | None:
        c = self._peek()
        if c not in _QUANTIFIER_STARTS:
            return None
        if c == "{":
            inner = self._bounds_text()
            if inner is None or not self._looks_like_bounds():
                raise self._fail("regex_syntax", "'{' must start a bounded repetition {n,m}")
            lo_s, _, hi_s = inner.partition(",")
            if len(lo_s) > 9 or len(hi_s) > 9:
                raise self._fail(
                    "regex_bound_too_large", f"a repetition bound may not exceed {QUANTIFIER_MAX}"
                )
            lo = int(lo_s)
            hi = int(hi_s) if hi_s else None
            if lo > QUANTIFIER_MAX or (hi is not None and hi > QUANTIFIER_MAX):
                raise self._fail(
                    "regex_bound_too_large", f"a repetition bound may not exceed {QUANTIFIER_MAX}"
                )
            if hi is not None and lo > hi:
                raise self._fail("regex_bound_inverted", "a repetition {n,m} needs n <= m")
            self.i += len(inner) + 2
            if "," in inner and hi is None:
                kind = "unbounded"
            else:
                kind = "mandatory" if lo >= 1 else "nullable"
        else:
            self.i += 1
            kind = "nullable" if c == "?" else "unbounded"
        if self._peek() == "?":  # lazy suffix
            self.i += 1
        nxt = self._peek()
        if nxt == "+":
            raise self._fail("regex_possessive_or_atomic", "possessive quantifiers are not allowed")
        if nxt in _QUANTIFIER_STARTS:
            raise self._fail("regex_stacked_quantifier", "a quantifier may not follow another")
        return kind


def validate_safe_regex(pattern: str, *, field: str, max_chars: int) -> None:
    """Validate ``pattern`` against the allowlist grammar, then compile it once.

    Raises :class:`ToolTriggerValidationError` with the stable code for the
    first violation found. Never evaluates the pattern against any input.
    """
    if len(pattern) > max_chars:
        raise ToolTriggerValidationError(
            "pattern_too_long", f"{field}: at most {max_chars} characters ({len(pattern)} given)"
        )
    for offset, ch in enumerate(pattern):
        if ord(ch) < 0x20:
            raise ToolTriggerValidationError(
                "pattern_control_char",
                f"{field}: control character U+{ord(ch):04X} at offset {offset}; "
                "use an escape such as \\t only if you mean a literal tab",
            )
    _Parser(pattern, field).parse()
    _compile_once(pattern, field)


def _parse_summary(pattern: str, field: str) -> _Seq:
    """Grammar pass only (no compile) — used to count literals for ``block``."""
    return _Parser(pattern, field).parse()


def _compile_once(pattern: str, field: str) -> None:
    # Called through the module object on purpose — the "never executed" test
    # replaces ``re.compile`` with a spy that returns a raising sentinel.
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ToolTriggerValidationError("regex_syntax", f"{field}: {exc}") from exc


# --------------------------------------------------------------------------- #
# details.tool_trigger normalization
# --------------------------------------------------------------------------- #


def normalize_tool_trigger(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate ``details.tool_trigger`` and write the normalized form back.

    Gate: fires only when the ``tool_trigger`` key is present (an orthogonal
    attribute — any memory type may be a guardrail). Absent key or ``None``
    details pass through untouched. An explicit ``"tool_trigger": null``
    REMOVES the key: ``details`` is a PostgreSQL ``json`` column, so a stored
    JSON ``null`` would still satisfy ``details->'tool_trigger' IS NOT NULL``
    and be indexed as a guardrail.

    Normalization writes the defaults back (``on``, ``action``) in the fixed
    key order ``tool, on, match?, action`` so every consumer sees explicit
    values; ``match`` is omitted when not supplied, never stored as ``null``.

    Raises:
        ToolTriggerValidationError: any structural or grammar violation.
    """
    if details is None or "tool_trigger" not in details:
        return details

    raw = details["tool_trigger"]
    if raw is None:
        return {k: v for k, v in details.items() if k != "tool_trigger"}
    if not isinstance(raw, dict):
        raise ToolTriggerValidationError(
            "tool_trigger_not_object",
            'details.tool_trigger must be an object like {"tool": "Bash|PowerShell", '
            '"match": "gh pr merge", "action": "inform"}',
        )
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise ToolTriggerValidationError(
            "tool_trigger_unknown_key",
            f"unknown key(s): {', '.join(unknown)} (allowed: {', '.join(sorted(_ALLOWED_KEYS))})",
        )

    tool = raw.get("tool")
    if tool is None or tool == "":
        raise ToolTriggerValidationError("tool_required", "tool_trigger.tool is required")
    if not isinstance(tool, str):
        raise ToolTriggerValidationError(
            "tool_not_string", f"tool_trigger.tool must be a string, got {type(tool).__name__}"
        )

    on = raw.get("on", TOOL_TRIGGER_DEFAULT_ON)
    if on not in TOOL_TRIGGER_ON:
        raise ToolTriggerValidationError(
            "on_invalid", f"tool_trigger.on must be one of {list(TOOL_TRIGGER_ON)}, got {on!r}"
        )
    action = raw.get("action", TOOL_TRIGGER_DEFAULT_ACTION)
    if action not in TOOL_TRIGGER_ACTIONS:
        raise ToolTriggerValidationError(
            "action_invalid",
            f"tool_trigger.action must be one of {list(TOOL_TRIGGER_ACTIONS)}, got {action!r}",
        )

    match: str | None = None
    if "match" in raw:
        match = raw["match"]
        if not isinstance(match, str):
            raise ToolTriggerValidationError(
                "match_not_string",
                f"tool_trigger.match must be a string, got {type(match).__name__}",
            )
        if match == "":
            raise ToolTriggerValidationError(
                "match_empty", "tool_trigger.match must not be empty (it would match everything)"
            )

    if action == "block":
        if on != "pre":
            raise ToolTriggerValidationError(
                "block_requires_pre", "action='block' is only allowed with on='pre'"
            )
        if match is None:
            raise ToolTriggerValidationError(
                "block_requires_match",
                "action='block' requires a match pattern so a block always names a specific input",
            )

    validate_safe_regex(tool, field="tool", max_chars=TOOL_PATTERN_MAX_CHARS)
    if match is not None:
        validate_safe_regex(match, field="match", max_chars=MATCH_PATTERN_MAX_CHARS)
        if action == "block" and _parse_summary(match, "match").literals == 0:
            raise ToolTriggerValidationError(
                "block_match_not_specific",
                "action='block' requires a match that names at least one literal character "
                "(a pattern made only of wildcards, classes or anchors would block every call)",
            )

    normalized: dict[str, Any] = {"tool": tool, "on": on}
    if match is not None:
        normalized["match"] = match
    normalized["action"] = action
    return {**details, "tool_trigger": normalized}


# --------------------------------------------------------------------------- #
# Served-set version
# --------------------------------------------------------------------------- #


def guardrail_version(entries: list[list[Any]]) -> str:
    """Hash of the served guardrail set (docs/mcp-tools.md § Tool guardrails).

    ``entries`` is ``[memory_id, summary, importance, delivery_mode,
    tool_trigger]`` per served item, pinned list first, in server order, after
    the binding filter (so it is per-credential). The canonical form is
    compact JSON with sorted keys; the first 16 hex digits of its SHA-256 are
    the version. Equal versions mean an unchanged cache; the value is
    otherwise opaque to clients.
    """
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
