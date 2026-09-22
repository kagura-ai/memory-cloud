"""Pure-logic tests for utils/tool_trigger.py — the ``details.tool_trigger`` contract.

Three things are pinned here, none needing a DB:

* the structural rules of the ``tool_trigger`` object (required keys, enums,
  lengths, the block-implies-pre / block-implies-match couplings), each with
  its stable error code;
* the safe-regex allowlist grammar — every rejected construct the contract
  names, plus the constructs Python's ``re`` accepts silently and the
  polynomial-backtracking shapes the nested-quantifier rule alone misses;
* the property the whole feature rests on: the server COMPILES a pattern to
  validate it and never evaluates it against any input. ``re.Pattern`` is an
  immutable C type, so this is pinned with a ``re.compile`` spy that returns a
  raising sentinel, and with an ``ast`` scan of the module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from utils import tool_trigger
from utils.tool_trigger import (
    GUARDRAIL_FORMAT,
    MATCH_PATTERN_MAX_CHARS,
    MAX_GROUP_DEPTH,
    MAX_UNBOUNDED_QUANTIFIERS,
    QUANTIFIER_MAX,
    TOOL_PATTERN_MAX_CHARS,
    TOOL_TRIGGER_ERROR_CODES,
    ToolTriggerValidationError,
    guardrail_version,
    normalize_tool_trigger,
    validate_safe_regex,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def _validate(pattern: str, *, max_chars: int = MATCH_PATTERN_MAX_CHARS) -> None:
    validate_safe_regex(pattern, field="match", max_chars=max_chars)


def _code(pattern: str, *, max_chars: int = MATCH_PATTERN_MAX_CHARS) -> str:
    with pytest.raises(ToolTriggerValidationError) as exc:
        _validate(pattern, max_chars=max_chars)
    assert exc.value.code in TOOL_TRIGGER_ERROR_CODES, exc.value.code
    # The message carries the code as its stable first token.
    assert str(exc.value).startswith(f"{exc.value.code}: ")
    return exc.value.code


# ------------------------------------------------------------- accepted corpus

ACCEPTED = [
    "Bash|PowerShell",
    "mcp__.*__remember",
    "Edit|Write",
    r"gh pr merge\b.*--delete-branch",
    r"(?i)git\s+push",
    "git (?:pull|merge) --ff-only",
    "a{2,100}",
    r"[^\s]+\.py$",
    r"\x41é",
    "\\u00e9",
    "a{0,}",
    "a{100}",
    "a+-b+",
    "a+b{1,20}c*",
    r"\d{4}-\d{2}",
    r"git\s+push\s+--force",
    # Forced splits: the closer is disjoint from the run before it, so a second
    # unbounded quantifier stays linear (see the ambiguous rows in REJECTED).
    r"\w+-\w+=",
    "[^-]+-[^-]+=",
    "(?:ab)+c(?:ab)+d",
    r"\w+ \w+ \w+ \w+=",
    r"\w+(?:-\w+)",
    r"(?:\w+-)\w+",
    r"(?:\w+)-\w+",
    r"a.*\nb.*c",  # '.' never matches a newline
    r"(?i)git\s+push\s+--force",
    r"(?:\w+a)",  # one ambiguous run alone is linear
    r"(?:\w+a)b",
    "[a-]+b[a-]+",
    "[a-]",
    "[-a]",
    "[a-z-]",
    "(?:ab)+c",
    "(?:ab)?c",
    "(?:ab){2,5}c",
    "^rm -rf",
    "a*?b",
    "a??b",
    "(a)(b)",
    "x{0,5}y",
    "[\\]\\[]",
    r"\.\*\+\?\(\)\[\]\{\}\|\^\$\/\-\\",
]
# ``\s*\S*`` is NOT accepted although the classes are disjoint: the adjacency
# rule is syntactic on purpose (see the module docstring) — it is in REJECTED.


@pytest.mark.parametrize("pattern", ACCEPTED)
def test_accepted_corpus(pattern):
    _validate(pattern)


def test_tool_field_boundary_lengths():
    validate_safe_regex(
        "a" * TOOL_PATTERN_MAX_CHARS, field="tool", max_chars=TOOL_PATTERN_MAX_CHARS
    )
    with pytest.raises(ToolTriggerValidationError) as exc:
        validate_safe_regex(
            "a" * (TOOL_PATTERN_MAX_CHARS + 1), field="tool", max_chars=TOOL_PATTERN_MAX_CHARS
        )
    assert exc.value.code == "pattern_too_long"
    assert "tool" in str(exc.value)


def test_match_field_boundary_lengths():
    _validate("a" * MATCH_PATTERN_MAX_CHARS)
    assert _code("a" * (MATCH_PATTERN_MAX_CHARS + 1)) == "pattern_too_long"


# ------------------------------------------------------------ rejected corpus

REJECTED = [
    # backreferences
    (r"(a)\1", "regex_backreference"),
    (r"\k<x>", "regex_backreference"),
    # lookaround
    ("(?=a)", "regex_lookaround"),
    ("(?!a)", "regex_lookaround"),
    ("(?<=a)b", "regex_lookaround"),
    ("(?<!a)b", "regex_lookaround"),
    # named groups
    ("(?P<n>a)", "regex_named_group"),
    ("(?<n>a)", "regex_named_group"),
    ("(?P=n)", "regex_named_group"),
    # atomic / possessive
    ("(?>a)", "regex_possessive_or_atomic"),
    ("a*+", "regex_possessive_or_atomic"),
    ("a++", "regex_possessive_or_atomic"),
    ("a?+", "regex_possessive_or_atomic"),
    ("a{1,2}+", "regex_possessive_or_atomic"),
    # inline flags other than one leading (?i)
    ("(?s)a", "regex_inline_flag"),
    ("(?m)a", "regex_inline_flag"),
    ("(?x)a", "regex_inline_flag"),
    ("(?i:a)", "regex_inline_flag"),
    ("(?-i)a", "regex_inline_flag"),
    ("(?i)(?i)a", "regex_inline_flag"),
    ("a(?i)b", "regex_inline_flag"),
    ("(?#c)a", "regex_inline_flag"),
    # unknown escapes
    (r"\A", "regex_unknown_escape"),
    (r"\Z", "regex_unknown_escape"),
    (r"\z", "regex_unknown_escape"),
    (r"\G", "regex_unknown_escape"),
    (r"\p{L}", "regex_unknown_escape"),
    (r"\0", "regex_unknown_escape"),
    (r"\Q", "regex_unknown_escape"),
    (r"\x4", "regex_unknown_escape"),
    (r"\u12", "regex_unknown_escape"),
    (r"[\b]", "regex_unknown_escape"),
    (r"\uD800", "regex_unknown_escape"),  # lone surrogate
    # bounds
    ("a{101}", "regex_bound_too_large"),
    ("a{2,101}", "regex_bound_too_large"),
    ("a{101,}", "regex_bound_too_large"),
    ("x{100000}", "regex_bound_too_large"),
    ("a{1000000000}", "regex_bound_too_large"),
    ("a{5,3}", "regex_bound_inverted"),
    # nested quantifiers / alternation under a quantifier
    ("(a+)+", "regex_nested_quantifier"),
    ("(a|ab)*", "regex_nested_quantifier"),
    ("(x(y*))?", "regex_nested_quantifier"),
    ("(a|b)?", "regex_nested_quantifier"),
    ("(?:a*){2}", "regex_nested_quantifier"),
    # stacked / dangling quantifiers
    ("a**", "regex_stacked_quantifier"),
    ("a+*", "regex_stacked_quantifier"),
    ("a{2}{3}", "regex_stacked_quantifier"),
    ("a???", "regex_stacked_quantifier"),
    ("*abc", "regex_dangling_quantifier"),
    ("(*)", "regex_dangling_quantifier"),
    ("a|+", "regex_dangling_quantifier"),
    (r"\b+", "regex_dangling_quantifier"),
    ("^*", "regex_dangling_quantifier"),
    # syntax
    ("(a", "regex_syntax"),
    ("a)", "regex_syntax"),
    ("[a", "regex_syntax"),
    ("a]", "regex_syntax"),
    ("a{", "regex_syntax"),
    ("a}", "regex_syntax"),
    ("a{,5}", "regex_syntax"),
    ("a{1,2", "regex_syntax"),
    ("abc\\", "regex_syntax"),
    ("", "regex_syntax"),
    ("a|", "regex_syntax"),
    ("|a", "regex_syntax"),
    ("(a|)", "regex_syntax"),
    (r"[a-\d]", "regex_syntax"),
    ("[z-a]", "regex_syntax"),
    # classes
    ("[[a]]", "regex_class_unsupported"),
    ("[a&&b]", "regex_class_unsupported"),
    ("[a--b]", "regex_class_unsupported"),
    ("[a~~b]", "regex_class_unsupported"),
    ("[a||b]", "regex_class_unsupported"),
    ("[]a]", "regex_class_empty"),
    ("[]", "regex_class_empty"),
    ("[^]", "regex_class_empty"),
    # empty group
    ("()", "regex_empty_group"),
    ("(?:)", "regex_empty_group"),
    # control characters, raw and escaped
    ("a\nb", "pattern_control_char"),
    ("a\x00b", "pattern_control_char"),
    (r"\x00", "pattern_control_char"),
    (r"\x1f", "pattern_control_char"),
    (r"\u0000", "pattern_control_char"),
    # adjacent unbounded quantifiers (polynomial backtracking)
    (".*.*", "regex_adjacent_unbounded"),
    (".*.*.*x", "regex_adjacent_unbounded"),
    (r"\w+\s*\w+=", "regex_adjacent_unbounded"),
    (r"\S+\s*\S+", "regex_adjacent_unbounded"),
    ("a+b+", "regex_adjacent_unbounded"),
    ("(?:a+)b*", "regex_adjacent_unbounded"),
    (r"\s*\S*", "regex_adjacent_unbounded"),
    ("a*?b*", "regex_adjacent_unbounded"),
    ("a+^b+", "regex_adjacent_unbounded"),  # zero-width atoms do not separate
    ("a+c?b+", "regex_adjacent_unbounded"),  # nullable atoms do not separate
    ("a+c{0,3}b+", "regex_adjacent_unbounded"),
    ("(?:a+|x)b*", "regex_adjacent_unbounded"),  # one branch ends unbounded
    ("(?:ab)+(?:cd)*", "regex_adjacent_unbounded"),
    ("a*(?:b*c)", "regex_adjacent_unbounded"),  # group starts unbounded
    # Ambiguous split: the atom that closes an unbounded run is one the run
    # could consume itself, and another unbounded quantifier follows. Measured
    # on an 8 KB subject: ``\w+a\w+=`` > 20 s, ``.*a.*b`` > 20 s, ``.*-.*=``
    # 19.5 s on "a-a-a…", ``\w+a{1,100}-\w+=`` 2.2 s. The forced-split twins in
    # ACCEPTED (``\w+-\w+=``, ``[^-]+-[^-]+=``) run in ≤ 0.05 s.
    (r"\w+a\w+=", "regex_ambiguous_separator"),
    (r"(\w+)a(\w+)=", "regex_ambiguous_separator"),
    (".*a.*b", "regex_ambiguous_separator"),
    (".*-.*=", "regex_ambiguous_separator"),
    (r"\w+a{1,100}-\w+=", "regex_ambiguous_separator"),  # overlapping bounded closer
    (r"(?:\w+a)\w+", "regex_ambiguous_separator"),  # ambiguity inside a group
    (r"\w+(?:a\w+)", "regex_ambiguous_separator"),  # group closes the run, overlapping
    (r"(?:x\w+)a\w+", "regex_ambiguous_separator"),  # run opened inside a group
    (r"\w+(?:ab)\w+", "regex_ambiguous_separator"),  # group first-set overlaps
    (r"\w+(?:-|x\w+)", "regex_ambiguous_separator"),  # one branch overlaps
    ("[a-z]+b[a-z]+", "regex_ambiguous_separator"),  # range membership
    ("[^-]+a[^-]+", "regex_ambiguous_separator"),  # negated class membership
    (r"(?i)A+a\w+", "regex_ambiguous_separator"),  # (?i) folds the literal
    (r".*a.*", "regex_ambiguous_separator"),  # \u escape resolves to 'a'
    (r"\S+\.py\s+\S+", "regex_ambiguous_separator"),  # '.' is in \S
    # too many unbounded quantifiers / too deep
    ("a+-b+-c+-d+-e+", "regex_too_many_unbounded"),
    ("(((((((((a)))))))))", "regex_nesting_too_deep"),
]


@pytest.mark.parametrize(("pattern", "code"), REJECTED)
def test_rejected_constructs(pattern, code):
    assert _code(pattern) == code


def test_every_documented_code_is_exercised_or_structural():
    """Every code in the shared dict has at least one test row — a renamed code
    cannot drift from the docs without a test failing."""
    exercised = {code for _, code in REJECTED} | {"pattern_too_long"}
    structural = {
        "tool_trigger_not_object",
        "tool_trigger_unknown_key",
        "tool_required",
        "tool_not_string",
        "match_not_string",
        "match_empty",
        "on_invalid",
        "action_invalid",
        "block_requires_pre",
        "block_requires_match",
        "block_match_not_specific",
        "block_match_nullable",
        "tool_trigger_requires_user_credential",
    }
    assert set(TOOL_TRIGGER_ERROR_CODES) == exercised | structural


def test_limits_are_the_documented_values():
    assert TOOL_PATTERN_MAX_CHARS == 128
    assert MATCH_PATTERN_MAX_CHARS == 200
    assert QUANTIFIER_MAX == 100
    assert MAX_UNBOUNDED_QUANTIFIERS == 4
    assert MAX_GROUP_DEPTH == 8
    assert GUARDRAIL_FORMAT == 1


# -------------------------------------------------------- structural rules


def _tt(**kwargs):
    return {"tool_trigger": kwargs}


def _struct_code(details) -> str:
    with pytest.raises(ToolTriggerValidationError) as exc:
        normalize_tool_trigger(details)
    return exc.value.code


def test_normalize_passthrough_without_key():
    details = {"other": 1}
    assert normalize_tool_trigger(details) is details
    assert normalize_tool_trigger(None) is None


def test_normalize_writes_defaults_in_canonical_key_order():
    out = normalize_tool_trigger({"x": 1, **_tt(tool="Bash")})
    assert out == {"x": 1, "tool_trigger": {"tool": "Bash", "on": "pre", "action": "inform"}}
    assert list(out["tool_trigger"]) == ["tool", "on", "action"]
    out = normalize_tool_trigger(_tt(action="block", match="gh pr merge", tool="Bash|PowerShell"))
    assert list(out["tool_trigger"]) == ["tool", "on", "match", "action"]
    assert out["tool_trigger"]["match"] == "gh pr merge"


def test_normalize_omits_match_when_not_supplied():
    out = normalize_tool_trigger(_tt(tool="Bash", on="result"))
    assert "match" not in out["tool_trigger"]


def test_normalize_explicit_null_removes_the_key():
    """A JSON ``null`` is NOT SQL NULL for ``details->'tool_trigger'``, so the
    unmark path must delete the key rather than store ``null``."""
    out = normalize_tool_trigger({"keep": 1, "tool_trigger": None})
    assert out == {"keep": 1}
    assert normalize_tool_trigger({"tool_trigger": None}) == {}


def test_normalize_does_not_mutate_input():
    details = _tt(tool="Bash")
    normalize_tool_trigger(details)
    assert details == {"tool_trigger": {"tool": "Bash"}}


@pytest.mark.parametrize(
    ("details", "code"),
    [
        ({"tool_trigger": "Bash"}, "tool_trigger_not_object"),
        ({"tool_trigger": ["Bash"]}, "tool_trigger_not_object"),
        (_tt(tool="Bash", extra=1), "tool_trigger_unknown_key"),
        (_tt(on="pre"), "tool_required"),
        (_tt(tool=""), "tool_required"),
        (_tt(tool=5), "tool_not_string"),
        (_tt(tool="a" * 129), "pattern_too_long"),
        (_tt(tool="Bash", match=5), "match_not_string"),
        (_tt(tool="Bash", match=None), "match_not_string"),
        (_tt(tool="Bash", match=""), "match_empty"),
        (_tt(tool="Bash", match="a" * 201), "pattern_too_long"),
        (_tt(tool="Bash", on="post"), "on_invalid"),
        (_tt(tool="Bash", on=None), "on_invalid"),
        (_tt(tool="Bash", action="deny"), "action_invalid"),
        (_tt(tool="Bash", action="block", match="rm", on="result"), "block_requires_pre"),
        (_tt(tool="Bash", action="block"), "block_requires_match"),
        (_tt(tool="Bash", action="block", match="."), "block_match_not_specific"),
        (_tt(tool="Bash", action="block", match=".*"), "block_match_not_specific"),
        (_tt(tool="Bash", action="block", match=r"\s*"), "block_match_not_specific"),
        (_tt(tool="Bash", action="block", match=r"[^x]*"), "block_match_not_specific"),
        (_tt(tool="Bash", action="block", match="^$"), "block_match_not_specific"),
        (_tt(tool="Bash", action="block", match=r"\d+"), "block_match_not_specific"),
        # A literal alone is not enough: an unanchored search with a pattern
        # that can match the empty string matches every subject.
        (_tt(tool="Bash", action="block", match="a*"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="a?"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="a{0,100}"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="a{0,}"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="(?:rm)?"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="(?:rm|x*)"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="^a*$"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match=r"\ba*\b"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="(?i)a*"), "block_match_nullable"),
        (_tt(tool="Bash", action="block", match="(?:(?:a?)*)"), "regex_nested_quantifier"),
        (_tt(tool="(a+)+"), "regex_nested_quantifier"),
        (_tt(tool="Bash", match="(a+)+"), "regex_nested_quantifier"),
        (_tt(tool="Bash\n"), "pattern_control_char"),
    ],
)
def test_structural_rules(details, code):
    assert _struct_code(details) == code


def test_block_with_a_literal_is_specific_enough():
    out = normalize_tool_trigger(_tt(tool="Bash", action="block", match=r"gh pr merge\b.*--delete"))
    assert out["tool_trigger"]["action"] == "block"
    # An escaped literal or a class with literals alongside counts too.
    normalize_tool_trigger(_tt(tool="Bash", action="block", match=r"\.env"))
    normalize_tool_trigger(_tt(tool="Bash", action="block", match=r"[Rr]m -rf"))


@pytest.mark.parametrize(
    "match",
    [
        "a+",  # an unbounded run is not a separator, but it is not nullable either
        "a{1,3}",
        "a{2,}",
        "(?:ab)+",
        "a*b",  # one mandatory atom is enough
        "(?:rm|del)",
        "(?:rm)?-rf",
        r"gh pr merge\b.*--delete",
    ],
)
def test_block_accepts_a_pattern_that_must_consume_input(match):
    out = normalize_tool_trigger(_tt(tool="Bash", action="block", match=match))
    assert out["tool_trigger"]["match"] == match


def test_nullable_match_is_fine_for_inform():
    """Only ``block`` rejects a nullable pattern — an ``inform`` that fires on
    every call is noisy, not a denial of service on the tool."""
    out = normalize_tool_trigger(_tt(tool="Bash", match="a*"))
    assert out["tool_trigger"] == {"tool": "Bash", "on": "pre", "match": "a*", "action": "inform"}


def test_structural_error_message_names_the_field():
    with pytest.raises(ToolTriggerValidationError) as exc:
        normalize_tool_trigger(_tt(tool="Bash", match="(a+)+"))
    assert "match" in str(exc.value)
    with pytest.raises(ToolTriggerValidationError) as exc:
        normalize_tool_trigger(_tt(tool="(a+)+"))
    assert "tool" in str(exc.value)


# ------------------------------------------------------------ never executed


class _RaisingPattern:
    """What ``re.compile`` hands back under the spy: anything but the two
    harmless attributes raises, so a ``.search`` / ``.match`` call fails loudly."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.flags = 0

    def __getattr__(self, name: str):
        # AttributeError is the contract for a lookup hook; the message still
        # names the unexpected execution path so a failure reads the same.
        raise AttributeError(f"pattern executed via .{name}()")


def test_compile_is_the_only_re_call(monkeypatch):
    calls: list[str] = []

    def spy(pattern, flags=0):
        calls.append(pattern)
        return _RaisingPattern(pattern)

    monkeypatch.setattr(tool_trigger.re, "compile", spy)

    for pattern in ACCEPTED:
        _validate(pattern)
    assert calls == ACCEPTED  # exactly once per accepted pattern, in order

    calls.clear()
    for pattern, _ in REJECTED:
        with pytest.raises(ToolTriggerValidationError):
            _validate(pattern)
    # Compile is the LAST step: a pattern the grammar rejects never reaches it.
    assert calls == []

    calls.clear()
    normalize_tool_trigger(_tt(tool="Bash|PowerShell", match="gh pr merge"))
    assert calls == ["Bash|PowerShell", "gh pr merge"]


def test_compile_failure_after_grammar_maps_to_regex_syntax(monkeypatch):
    def broken(pattern, flags=0):
        raise re.error("boom")

    monkeypatch.setattr(tool_trigger.re, "compile", broken)
    assert _code("abc") == "regex_syntax"


_EXECUTING_ATTRS = {
    "search",
    "match",
    "fullmatch",
    "finditer",
    "findall",
    "sub",
    "subn",
    "split",
    "scanner",
}


def test_module_has_no_match_call_sites():
    tree = ast.parse((SRC / "utils" / "tool_trigger.py").read_text(encoding="utf-8"))
    offenders = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in _EXECUTING_ATTRS
    ]
    assert offenders == [], offenders
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name and ("sre_parse" in name or "re._parser" in name) for name in imports)


@pytest.mark.parametrize(
    "relative",
    ["repositories/memory.py", "services/memory_service.py", "mcp_server/tools/memory.py"],
)
def test_read_path_modules_never_compile_a_pattern(relative):
    """The read lane returns ``tool_trigger`` as data. A "helpful" server-side
    pre-check that compiled or ran a stored pattern would reintroduce the
    attack surface this feature keeps off the server."""
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    ]
    assert hits == []


# --------------------------------------------------------------- version hash


def test_guardrail_version_golden_vector():
    entries = [
        ["11111111-1111-1111-1111-111111111111", "goal", 0.9, "always", None],
        [
            "22222222-2222-2222-2222-222222222222",
            "safe alternative",
            0.8,
            "on_recall",
            {"tool": "Bash", "on": "pre", "match": "x", "action": "inform"},
        ],
    ]
    version = guardrail_version(entries)
    assert re.fullmatch(r"[0-9a-f]{16}", version)
    # Pinned by value so a refactor cannot silently change the canonical form.
    assert version == "cea58cd5a92d4baf"


def test_guardrail_version_is_order_sensitive_and_content_sensitive():
    a = ["1", "s", 0.5, "always", None]
    b = ["2", "t", 0.5, "on_recall", {"tool": "Bash", "on": "pre", "action": "inform"}]
    assert guardrail_version([a, b]) != guardrail_version([b, a])
    assert guardrail_version([a]) != guardrail_version([["1", "s2", 0.5, "always", None]])
    assert guardrail_version([]) == guardrail_version([])
