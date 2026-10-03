"""#1831: no OAuth route does sync DB work on the event loop.

``get_sync_session()`` blocks the loop for the whole query — and, when the
row it reads is locked by an async transaction of the same process, blocks
the loop that would have to commit that transaction (#1770). Every call in
``api/routes/oauth.py`` must therefore sit in a plain ``def`` that the route
hands to ``_run_sync`` (or ``asyncio.to_thread``): a module-level helper, or
a nested body. A call inside an ``async def``, or a nested body that the
route calls directly instead of handing to the worker pool, fails this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

OAUTH_ROUTES = Path(__file__).resolve().parents[2] / "src" / "api" / "routes" / "oauth.py"
THREAD_RUNNERS = {"_run_sync", "to_thread"}


def _is_sync_session_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "get_sync_session"


def _calls_with_chains(tree: ast.Module) -> list[tuple[int, list[ast.AST]]]:
    """(line, chain of enclosing function defs, innermost last) per call."""
    found: list[tuple[int, list[ast.AST]]] = []

    def visit(node: ast.AST, chain: list[ast.AST]) -> None:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            chain = [*chain, node]
        if _is_sync_session_call(node):
            found.append((node.lineno, chain))
        for child in ast.iter_child_nodes(node):
            visit(child, chain)

    visit(tree, [])
    return found


def _runner_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _handed_to_worker_pool(route: ast.AST, body: ast.FunctionDef) -> bool:
    """Every reference to the nested ``body`` inside ``route`` is an argument of
    ``_run_sync(...)`` / ``asyncio.to_thread(...)`` — never a direct call."""
    handed = 0
    for node in ast.walk(route):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == body.name:
                return False  # called directly: runs on the loop
            if _runner_name(node) in THREAD_RUNNERS and any(
                isinstance(arg, ast.Name) and arg.id == body.name for arg in node.args
            ):
                handed += 1
    return handed > 0


def test_every_sync_session_is_opened_off_the_loop():
    tree = ast.parse(OAUTH_ROUTES.read_text(encoding="utf-8"))
    calls = _calls_with_chains(tree)
    assert calls, "expected get_sync_session() calls in oauth.py"

    offenders: dict[int, str] = {}
    for line, chain in calls:
        if not chain or isinstance(chain[-1], ast.AsyncFunctionDef):
            offenders[line] = f"{chain[-1].name if chain else '<module>'}: on the event loop"
            continue
        body = chain[-1]
        # A nested body: find the async route that encloses it and check it is
        # handed to the worker pool rather than called.
        route = next((n for n in reversed(chain[:-1]) if isinstance(n, ast.AsyncFunctionDef)), None)
        if route is not None and not _handed_to_worker_pool(route, body):
            offenders[line] = f"{route.name}.{body.name}: nested body not run via _run_sync"
    assert offenders == {}, (
        "sync DB work reaches the event loop: "
        + "; ".join(f"line {line}: {why}" for line, why in sorted(offenders.items()))
        + " — move it into a def handed to _run_sync (#1831)"
    )


def test_the_routes_use_the_bounded_oauth_pool_not_the_default_one():
    """The sync bodies share one bounded executor (#1831 review): the default
    ``to_thread`` pool could park more threads on a row lock than the sync
    engine has connections."""
    source = OAUTH_ROUTES.read_text(encoding="utf-8")
    assert "asyncio.to_thread(_sync)" not in source
    assert source.count("await _run_sync(_sync)") >= 13
    assert "_OAUTH_SYNC_EXECUTOR = ThreadPoolExecutor(" in source
