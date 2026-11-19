"""Every place that verifies a key, pinned by name (#2929).

A connector key is accepted only where ``auth.connector_key_scope`` accepts it.
That holds as long as every place that verifies an API key applies the rule or
cannot hand a connector key a principal. This module walks ``backend/src`` and
pins every ``.verify_key(...)`` call and every caller of
``auth.dependencies.verify_api_key`` to a literal set of (file, function). A new
caller fails here until it is added below, next to a sentence saying how it
treats a connector-scoped key.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# (file under src, enclosing function) -> how it treats a connector-scoped key.
VERIFY_KEY_CALLS: dict[tuple[str, str], str] = {
    ("auth/dependencies.py", "verify_api_key"): (
        "The MCP wrapper: the MCP auth seam records the connector scope from the "
        "result and the tool dispatcher applies it to every tools/call."
    ),
    ("auth/dependencies.py", "verify_api_key_user"): (
        "REST: _apply_connector_key_scope runs before the principal is built."
    ),
    ("auth/dependencies.py", "get_user_from_api_key_or_session"): (
        "REST: _apply_connector_key_scope runs before the principal is built and "
        "a refusal never falls back to the session."
    ),
    ("auth/dependencies.py", "get_share_key_principal"): (
        "ShareKeyManager.verify_key: the share_keys store, where no API key verifies."
    ),
    ("api/routes/public_search.py", "_resolve_public_attribution"): (
        "Accepts a public-bound key only; a connector key is workspace-scoped, never "
        "bound, so it is refused with 403 before any principal exists."
    ),
}

VERIFY_API_KEY_CALLERS: dict[tuple[str, str], str] = {
    ("mcp_server/auth.py", "_verify_api_key"): (
        "Sets the per-request connector scope (VerifiedKey.connector_scoped) that "
        "tools/call, tools/list and the instructions read."
    ),
}


def _matches(call: ast.Call, attribute: str | None, name: str | None) -> bool:
    """``x.<attribute>(...)``; or ``<name>(...)`` and ``<module>.<name>(...)``."""
    func = call.func
    if attribute and isinstance(func, ast.Attribute):
        return func.attr == attribute
    if not name:
        return False
    if isinstance(func, ast.Name):
        return func.id == name
    return isinstance(func, ast.Attribute) and func.attr == name


def _visit(
    node: ast.AST,
    function: str,
    where: str,
    attribute: str | None,
    name: str | None,
    found: set[tuple[str, str]],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _visit(child, child.name, where, attribute, name, found)
            continue
        if isinstance(child, ast.Call) and _matches(child, attribute, name):
            found.add((where, function))
        _visit(child, function, where, attribute, name, found)


def _calls(attribute: str | None, name: str | None) -> set[tuple[str, str]]:
    """(file, enclosing function) of every call ``x.<attribute>(...)`` or ``<name>(...)``."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _visit(tree, "<module>", path.relative_to(SRC).as_posix(), attribute, name, found)
    return found


def test_every_verify_key_call_is_pinned() -> None:
    assert _calls("verify_key", None) == set(VERIFY_KEY_CALLS)


def test_every_verify_api_key_caller_is_pinned() -> None:
    callers = _calls(None, "verify_api_key") - {("auth/dependencies.py", "verify_api_key")}
    assert callers == set(VERIFY_API_KEY_CALLERS)
