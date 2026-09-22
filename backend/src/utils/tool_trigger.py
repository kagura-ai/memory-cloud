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
quantifier on a group that itself contains a quantifier or ``|``, two
unbounded quantifiers with nothing mandatory between them (``\\w+\\s*\\w+``
is polynomial in every backtracking engine even though no quantifier is
nested), and two unbounded quantifiers whose separator the first one can
consume itself (``\\w+a\\w+=`` and ``.*a.*b`` take > 20 s on an 8 KB subject
because the first run's end is not forced; ``\\w+-\\w+=`` takes 0.05 s).
``sre_parse`` is deliberately not used: it accepts Python-only syntax we
would have to walk and reject anyway, and it is private API.

What the grammar guarantees is the absence of ambiguous splits between
unbounded runs — the source of super-linear backtracking in a pattern without
nested quantifiers. It does not make a backtracking engine linear in every
case (``re.search`` still retries each start position), which is why the
client's 8 KB subject cap and per-pattern time budget stay normative.

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
    "regex_ambiguous_separator": (
        "an unbounded quantifier followed by another must be closed by an atom it cannot "
        "match itself"
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

# The sample alphabet the set-overlap rule (``regex_ambiguous_separator``)
# reasons over: printable ASCII, the escapable whitespace, a few non-ASCII
# representatives (a Latin letter, an Arabic-Indic digit, NBSP, LINE
# SEPARATOR, a kana) — plus, per pattern, every code point the pattern itself
# names (see ``_alphabet``), so a literal outside this base can still collide
# with the class next to it. Class membership uses Python ``str`` semantics,
# a superset of the ASCII-only JavaScript sets: more overlap, never less.
_BASE_ALPHABET = (
    frozenset(chr(c) for c in range(0x20, 0x7F)) | frozenset("\t\n\r\f\v") | frozenset("é٣  あ")
)


def _alphabet(pattern: str) -> frozenset[str]:
    """``_BASE_ALPHABET`` plus the pattern's own literals (raw, ``\\xHH``,
    ``\\uHHHH``) and their case partners, for ``(?i)``. Hand-scanned — this
    module never runs a regex, not even on the pattern itself."""
    chars = set(_BASE_ALPHABET)
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n and pattern[i + 1] in "xu":
            width = 2 if pattern[i + 1] == "x" else 4
            digits = pattern[i + 2 : i + 2 + width]
            if len(digits) == width and all(d in _HEX for d in digits):
                chars.add(chr(int(digits, 16)))
            i += 2 + width
            continue
        chars.add(c)
        i += 1
    for c in list(chars):
        swapped = c.swapcase()
        if len(swapped) == 1:
            chars.add(swapped)
    return frozenset(chars)


def _class_escape_set(letter: str, alphabet: frozenset[str]) -> frozenset[str]:
    """Members of ``\\d \\w \\s`` (and the negated ``\\D \\W \\S``) in ``alphabet``."""
    base = letter.lower()
    if base == "d":
        positive = frozenset(x for x in alphabet if x.isdigit())
    elif base == "w":
        positive = frozenset(x for x in alphabet if x.isalnum() or x == "_")
    else:
        positive = frozenset(x for x in alphabet if x.isspace())
    return positive if letter.islower() else alphabet - positive


@dataclass
class _Atom:
    """What the sequence walker needs to know about one parsed element."""

    kind: str  # "atom" | "zero_width" | "group"
    literal: bool = False
    # The characters this atom can consume first (its first-set): the set of a
    # literal / class / ``.``, or the union of a group's branch first-sets.
    # Empty for zero-width atoms.
    chars: frozenset[str] = frozenset()
    # group-only: how the body behaves at its edges (see _Seq)
    starts_unbounded: bool = False
    ends_unbounded: bool = False
    end_set: frozenset[str] = frozenset()
    mandatory: bool = False
    ambiguous: bool = False
    has_quantifier: bool = False
    has_unbounded: bool = False
    has_alternation: bool = False


@dataclass
class _Seq:
    """Summary of one alternative (a ``sequence``) or a whole group body.

    ``starts_unbounded`` — before any mandatory atom, an unbounded one occurs.
    ``ends_unbounded`` — an unbounded run is still open at the end; ``end_set``
    is that run's first-set.
    ``mandatory`` — at least one atom must consume input (so the sequence can
    separate two unbounded runs around it).
    ``first_set`` — every character the sequence can consume first.
    ``ambiguous`` — an unbounded run was closed by an atom it could itself
    match, so the run's end position is not forced (``\\w+a``); a later
    unbounded quantifier in the same sequence is then polynomial.
    """

    starts_unbounded: bool = False
    ends_unbounded: bool = False
    end_set: frozenset[str] = frozenset()
    mandatory: bool = False
    first_set: frozenset[str] = frozenset()
    ambiguous: bool = False
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
        self.ci = pattern.startswith("(?i)")
        self.alphabet = _alphabet(pattern)

    # -- helpers ---------------------------------------------------------

    def _fail(self, code: str, sentence: str) -> ToolTriggerValidationError:
        return ToolTriggerValidationError(code, f"{self.field}: {sentence} (at offset {self.i})")

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.p[j] if j < self.n else ""

    def _literal_set(self, c: str) -> frozenset[str]:
        if not self.ci:
            return frozenset({c})
        lc = c.lower()
        return frozenset(x for x in self.alphabet if x.lower() == lc)

    def _range_set(self, lo: str, hi: str) -> frozenset[str]:
        if not self.ci:
            return frozenset(x for x in self.alphabet if lo <= x <= hi)
        return frozenset(
            x
            for x in self.alphabet
            if lo <= x <= hi or lo <= x.lower() <= hi or lo <= x.upper() <= hi
        )

    # -- entry -----------------------------------------------------------

    def parse(self) -> _Seq:
        if self.ci:
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
            end_set=frozenset().union(*(b.end_set for b in branches)),
            mandatory=all(b.mandatory for b in branches),
            first_set=frozenset().union(*(b.first_set for b in branches)),
            ambiguous=any(b.ambiguous for b in branches),
            has_quantifier=any(b.has_quantifier for b in branches),
            has_alternation=len(branches) > 1 or any(b.has_alternation for b in branches),
            literals=sum(b.literals for b in branches),
        )
        return merged

    def _sequence(self, depth: int) -> _Seq:
        """Walk one alternative, enforcing the backtracking rules.

        Two runs of state ride along the atoms:

        * ``pending_unbounded`` / ``run_set`` — an unbounded quantifier
          (``*``, ``+``, ``{n,}``) is open and this is its first-set. The next
          consuming atom closes it (``regex_adjacent_unbounded`` if that atom
          is itself unbounded; nullable and zero-width atoms do not close).
        * ``ambiguous`` — a run was closed by an atom the run could itself
          consume (``\\w+a``, ``.*-``), so the engine has O(n) candidate end
          positions for it. One such run is linear; a further unbounded
          quantifier in the same sequence multiplies the candidates
          (``\\w+a\\w+=`` > 20 s on 8 KB) → ``regex_ambiguous_separator``.
          A closer disjoint from the run (``\\w+-``, ``\\s+p``) forces the
          split, and the pair stays linear.
        """
        seq = _Seq()
        pending_unbounded = False
        run_set: frozenset[str] = frozenset()
        ambiguous = False
        seen_mandatory = False
        first_open = True
        count = 0
        adjacent = "two unbounded quantifiers need a mandatory atom between them"
        overlap = (
            "an unbounded quantifier followed by another must be closed by an atom it "
            "cannot match itself (\\w+-\\w+ is fine; \\w+a\\w+ and .*a.*b are not)"
        )
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
            if first_open:
                seq.first_set = seq.first_set | atom.chars

            # An unquantified group is walked by its edges: it may open, close
            # or carry an unbounded run of its own.
            if atom.kind == "group" and quant is None:
                if pending_unbounded and (
                    atom.starts_unbounded or (not atom.mandatory and atom.has_unbounded)
                ):
                    raise self._fail("regex_adjacent_unbounded", adjacent)
                if atom.mandatory and pending_unbounded and (atom.chars & run_set):
                    ambiguous = True
                if atom.has_unbounded and ambiguous:
                    raise self._fail("regex_ambiguous_separator", overlap)
                ambiguous = ambiguous or atom.ambiguous
                if atom.starts_unbounded and not seen_mandatory:
                    seq.starts_unbounded = True
                if atom.mandatory:
                    pending_unbounded = False
                    seen_mandatory = True
                    first_open = False
                if atom.ends_unbounded:
                    pending_unbounded = True
                    run_set = atom.end_set
                continue

            # A quantified group has a flat body (the nested rule above), so it
            # behaves as one atom of its quantifier's class.
            if quant == "unbounded":
                if pending_unbounded:
                    raise self._fail("regex_adjacent_unbounded", adjacent)
                if ambiguous:
                    raise self._fail("regex_ambiguous_separator", overlap)
                self.unbounded_count += 1
                if self.unbounded_count > MAX_UNBOUNDED_QUANTIFIERS:
                    raise self._fail(
                        "regex_too_many_unbounded",
                        f"at most {MAX_UNBOUNDED_QUANTIFIERS} unbounded quantifiers per pattern",
                    )
                pending_unbounded = True
                run_set = atom.chars
                if not seen_mandatory:
                    seq.starts_unbounded = True
            elif quant == "nullable":
                pass  # ?, {0,m}: consumes nothing for sure — does not separate
            else:  # None or "mandatory"
                if pending_unbounded and (atom.chars & run_set):
                    ambiguous = True
                pending_unbounded = False
                seen_mandatory = True
                first_open = False

        if count == 0:
            raise self._fail("regex_syntax", "empty pattern or empty alternative")
        seq.ends_unbounded = pending_unbounded
        seq.end_set = run_set if pending_unbounded else frozenset()
        seq.mandatory = seen_mandatory
        seq.ambiguous = ambiguous
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
            # Python's ``.`` (everything but ``\n``) is a superset of the
            # JavaScript one — the conservative side for the overlap rule.
            return _Atom(kind="atom", chars=self.alphabet - frozenset("\n"))
        if c == ")":
            raise self._fail("regex_syntax", "unbalanced ')'")
        if c == "]":
            raise self._fail("regex_syntax", "unbalanced ']'")
        if c == "}":
            raise self._fail("regex_syntax", "unbalanced '}'")
        self.i += 1
        return _Atom(kind="atom", literal=True, chars=self._literal_set(c))

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
        unbounded_before = self.unbounded_count
        body = self._alternation(depth)
        if self._peek() != ")":
            raise self._fail("regex_syntax", "unbalanced '('")
        self.i += 1
        return _Atom(
            kind="group",
            literal=body.literals > 0,
            chars=body.first_set,
            starts_unbounded=body.starts_unbounded,
            ends_unbounded=body.ends_unbounded,
            end_set=body.end_set,
            mandatory=body.mandatory,
            ambiguous=body.ambiguous,
            has_quantifier=body.has_quantifier,
            has_unbounded=self.unbounded_count > unbounded_before,
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
        members: set[str] = set()
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
            lo, lo_set = self._class_item()
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
                hi, _ = self._class_item()
                if hi is None:
                    raise self._fail("regex_syntax", "a range needs a literal on both ends")
                if ord(lo) > ord(hi):
                    raise self._fail("regex_syntax", "inverted range in [...]")
                members |= self._range_set(lo, hi)
            else:
                members |= lo_set
            first = False
        chars = frozenset(members)
        if negated:
            chars = self.alphabet - chars
        # A negated class names what NOT to match; it does not make a block
        # pattern specific (``[^x]*`` matches almost everything).
        return _Atom(kind="atom", literal=literal and not negated, chars=chars)

    def _class_item(self) -> tuple[str | None, frozenset[str]]:
        """One class member → ``(literal_char | None, its character set)``;
        the literal is None for a class escape such as ``\\d``."""
        c = self._peek()
        if c == "\\":
            atom, resolved = self._escape(in_class=True)
            return resolved, atom.chars
        self.i += 1
        return c, self._literal_set(c)

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
            return _Atom(kind="atom", chars=_class_escape_set(c, self.alphabet)), None
        if c in _ZERO_WIDTH_ESCAPES:
            if in_class:
                self.i = start
                raise self._fail("regex_unknown_escape", "\\b and \\B are not allowed in [...]")
            return _Atom(kind="zero_width"), None
        if c in _LITERAL_ESCAPES:
            resolved = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}.get(c, c)
            return _Atom(kind="atom", literal=True, chars=self._literal_set(resolved)), resolved
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
            resolved = chr(point)
            return _Atom(kind="atom", literal=True, chars=self._literal_set(resolved)), resolved
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
