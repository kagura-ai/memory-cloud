"""The declared enforcement mode of every plan feature must match the code (#1648).

``FEATURE_MIN_PLANS`` reads like the registry of plan-gated features, but a row
there only says which tier owns a feature — it is NOT evidence that anything
refuses a tier without it. ``FEATURE_ENFORCEMENT`` (#1648) makes that explicit,
and this module is the guard that keeps the label honest: it scans
``backend/src`` for the two gate entry points (``has_feature`` and
``QuotaService.check_feature_access``) and compares the call sites it finds with
the declared mode.

The scan is deliberately source-level. A runtime probe would have to exercise
every gate against every tier; what we actually want to catch is a *declaration*
drifting away from the code — a feature quietly losing its gate, or gaining one
without the registry (and the web UI reading it) being told.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from config.plan_tiers import (
    FEATURE_ENFORCEMENT,
    KNOWN_FEATURES,
    FeatureEnforcement,
    feature_enforcement,
    feature_enforcement_modes,
)

# backend/src — plan_tiers.py lives at src/config/plan_tiers.py.
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
REGISTRY_FILE = SRC_ROOT / "config" / "plan_tiers.py"

# The two ways backend code asks "does this plan include X?".
GATE_CALLEES = frozenset({"has_feature", "check_feature_access"})


def _callee_name(call: ast.Call) -> str | None:
    """Bare name of the function being called (``a.b.c(...)`` → ``"c"``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_string_constants(trees: dict[Path, ast.Module]) -> dict[str, str]:
    """UPPER_CASE module-level ``NAME = "value"`` bindings across the tree.

    Gate calls do not always pass a literal — ``llm_lane`` calls
    ``has_feature(plan, MANAGED_LLM_FEATURE)``. The map is global (not
    per-file) because the constant is usually imported, and an imported name
    keeps its spelling. A name bound to two different strings is dropped
    rather than guessed at.
    """
    values: dict[str, str] = {}
    ambiguous: set[str] = set()
    for tree in trees.values():
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.isupper():
                    continue
                previous = values.get(target.id)
                if previous is not None and previous != node.value.value:
                    ambiguous.add(target.id)
                values[target.id] = node.value.value
    for name in ambiguous:
        values.pop(name, None)
    return values


def _string_arg(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The string an argument evaluates to, when that is statically knowable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute):
        return constants.get(node.attr)
    return None


@pytest.fixture(scope="module")
def gate_call_sites() -> dict[str, list[str]]:
    """``feature name -> ["relative/path.py:lineno", ...]`` for every gate call."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if path != REGISTRY_FILE
    }
    constants = _module_string_constants(trees)

    sites: dict[str, list[str]] = defaultdict(list)
    for path, tree in trees.items():
        relative = path.relative_to(SRC_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee_name(node) not in GATE_CALLEES:
                continue
            arguments = list(node.args) + [keyword.value for keyword in node.keywords]
            for argument in arguments:
                value = _string_arg(argument, constants)
                if value in KNOWN_FEATURES:
                    sites[str(value)].append(f"{relative}:{node.lineno}")
    return dict(sites)


def test_scanner_finds_the_known_gates(gate_call_sites: dict[str, list[str]]) -> None:
    """Guard the guard: a scanner that finds nothing would pass every check below."""
    assert gate_call_sites, (
        "No has_feature / check_feature_access call sites found under backend/src. "
        "The scanner in this module is broken (moved source tree? renamed gate "
        "helper?) — fix it before trusting the assertions that follow."
    )
    # A literal and a constant-bound argument, so both resolution paths are live.
    assert gate_call_sites.get("team_invitations"), "literal-argument resolution broke"
    assert gate_call_sites.get("managed_llm"), "constant-argument resolution broke"


def test_every_known_feature_declares_a_mode() -> None:
    missing = sorted(KNOWN_FEATURES - set(FEATURE_ENFORCEMENT))
    extra = sorted(set(FEATURE_ENFORCEMENT) - KNOWN_FEATURES)
    assert not missing, (
        f"Feature(s) {', '.join(missing)} have no FEATURE_ENFORCEMENT row. Add one in "
        "config/plan_tiers.py saying what the feature does at runtime: 'enforced' (a "
        "check refuses), 'conditional' (a check refuses only where a deployment setting "
        "turns it on), 'degrades' (the request still succeeds) or 'advertised' (no "
        "runtime check at all)."
    )
    assert not extra, (
        f"FEATURE_ENFORCEMENT declares {', '.join(extra)}, which no tier and no "
        "FEATURE_MIN_PLANS row knows. Drop the row, or add the feature to a tier."
    )


@pytest.mark.parametrize(
    "feature",
    sorted(FEATURE_ENFORCEMENT),
)
def test_declared_mode_matches_the_call_sites(
    feature: str, gate_call_sites: dict[str, list[str]]
) -> None:
    """Enforced/degrading features must be checked somewhere; advertised ones must not."""
    mode = FEATURE_ENFORCEMENT[feature].mode
    sites = gate_call_sites.get(feature, [])

    if mode is FeatureEnforcement.ADVERTISED:
        assert not sites, (
            f"'{feature}' is declared ADVERTISED (no runtime check) but is checked at: "
            f"{', '.join(sites)}. Either the gate is new — change the row in "
            "config/plan_tiers.py to ENFORCED, CONDITIONAL or DEGRADES, and make sure "
            "the web UI gates accordingly — or the check is a leftover and should be "
            "removed."
        )
    else:
        assert sites, (
            f"'{feature}' is declared {mode.value.upper()} but nothing under backend/src "
            "calls has_feature / check_feature_access for it, so no tier is actually "
            "treated differently. Either restore the gate, or change the row in "
            "config/plan_tiers.py to ADVERTISED so the plan API stops telling clients "
            "this feature is gated."
        )


def test_every_row_explains_itself() -> None:
    """A mode without a reason is the same opacity #1648 exists to remove."""
    for feature, gate in sorted(FEATURE_ENFORCEMENT.items()):
        assert len(gate.note.strip()) >= 20, (
            f"FEATURE_ENFORCEMENT['{feature}'].note must say where the gate lives (or "
            "why there is none) — a reader deciding whether a UI may hard-disable a "
            "control has nothing else to go on."
        )


def test_known_unenforced_entries_stay_visible() -> None:
    """The #1648 findings, pinned.

    These three are declared, listed on the plan pages and checked by nothing.
    Enforcing them would start refusing requests that work today and deleting
    them would change what the plan endpoints advertise — both product calls.
    If one of them ever becomes a real gate, this test is the reminder to
    update the mode, the docs and the web UI in the same change.
    """
    for feature in ("api_keys", "oauth", "secret_store"):
        assert feature_enforcement(feature) is FeatureEnforcement.ADVERTISED, feature

    # Reranking is checked, but only degrades: recall succeeds unreranked.
    assert feature_enforcement("reranking") is FeatureEnforcement.DEGRADES


def test_managed_embeddings_is_conditional_not_enforced() -> None:
    """The gate exists but is off by default, so it must not read as ENFORCED.

    ``services/embedding_service.platform_fallback_allowed`` returns ``True``
    for every tier unless ``EMBEDDING_PLATFORM_FALLBACK_REQUIRES_MANAGED_PLAN``
    is set, and that setting defaults to ``False``. A client that hard-gates
    every ``enforced`` feature would therefore refuse managed embeddings in a
    deployment where the backend embeds happily — the exact kind of invented
    gate #1648 exists to prevent. If the default ever flips, change the mode to
    ENFORCED here and in ``docs/deployment.md`` in the same change.
    """
    from config.settings import Settings

    assert Settings.model_fields["embedding_platform_fallback_requires_managed_plan"].default is (
        False
    ), "the default flipped — managed_embeddings may now be ENFORCED; update the row"
    assert feature_enforcement("managed_embeddings") is FeatureEnforcement.CONDITIONAL


def test_unknown_feature_reads_as_advertised() -> None:
    """Fail-soft: a name the registry does not know must not look like a gate."""
    assert feature_enforcement("not_a_feature") is FeatureEnforcement.ADVERTISED


def test_modes_serialize_as_plain_sorted_strings() -> None:
    modes = feature_enforcement_modes()
    assert list(modes) == sorted(modes)
    assert set(modes) == set(FEATURE_ENFORCEMENT)
    assert set(modes.values()) <= {"enforced", "conditional", "degrades", "advertised"}
    assert all(isinstance(value, str) and type(value) is str for value in modes.values())
