"""#1831: no OAuth route does sync DB work on the event loop.

``get_sync_session()`` blocks the loop for the whole query — and, when the
row it reads is locked by an async transaction of the same process, blocks
the loop that would have to commit that transaction (#1770). Every call in
``api/routes/oauth.py`` must therefore sit in a plain ``def``: a helper the
routes run through ``asyncio.to_thread``, or the nested ``_sync`` body of a
route. A new call inside an ``async def`` fails this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

OAUTH_ROUTES = Path(__file__).resolve().parents[2] / "src" / "api" / "routes" / "oauth.py"


def _enclosing_functions(tree: ast.Module) -> dict[int, list[ast.AST]]:
    """Line → the chain of function definitions that contain it, innermost last."""
    found: dict[int, list[ast.AST]] = {}

    def visit(node: ast.AST, chain: list[ast.AST]) -> None:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            chain = [*chain, node]
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_sync_session":
            found[node.lineno] = chain
        for child in ast.iter_child_nodes(node):
            visit(child, chain)

    visit(tree, [])
    return found


def test_every_sync_session_is_opened_in_a_plain_function():
    tree = ast.parse(OAUTH_ROUTES.read_text(encoding="utf-8"))
    calls = _enclosing_functions(tree)
    assert calls, "expected get_sync_session() calls in oauth.py"
    on_the_loop = {
        line: chain[-1].name
        for line, chain in calls.items()
        if not chain or isinstance(chain[-1], ast.AsyncFunctionDef)
    }
    assert on_the_loop == {}, (
        f"get_sync_session() opened on the event loop at lines {sorted(on_the_loop)} "
        f"({sorted(set(on_the_loop.values()))}); move the sync work into a def run via "
        "asyncio.to_thread (#1831)"
    )


@pytest.mark.parametrize(
    "route",
    [
        "list_oauth2_clients",
        "create_oauth2_client",
        "dynamic_client_registration",
        "get_oauth2_client",
        "update_oauth2_client",
        "hide_oauth2_client_secret",
        "regenerate_oauth2_client_secret",
        "delete_oauth2_client",
        "oauth_authorize_get",
        "device_authorize",
        "device_verify",
        "introspect_token",
        "oauth_revoke",
    ],
)
def test_the_converted_routes_run_their_sync_body_in_a_thread(route: str):
    tree = ast.parse(OAUTH_ROUTES.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == route)
    source = ast.unparse(fn)
    assert "def _sync(" in source and "await asyncio.to_thread(_sync)" in source, route
