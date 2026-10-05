"""#1831: no OAuth route does sync DB work on the event loop.

``get_sync_session()`` blocks the loop for the whole query — and, when the
row it reads is locked by an async transaction of the same process, blocks
the loop that would have to commit that transaction (#1770). Every call in
``api/routes/oauth.py`` must therefore sit in a plain ``def`` that runs in a
worker thread: a nested body the route awaits through ``_run_sync`` (the
bounded OAuth pool), or a module-level helper. A call inside an ``async def``,
a nested body that the route calls directly, and a nested body handed to
``asyncio.to_thread`` (the default pool) all fail this module.

The only ``asyncio.to_thread`` targets allowed are the module-level helpers
in ``DEFAULT_POOL_HELPERS``; the comment above ``_OAUTH_SYNC_EXECUTOR`` says
why each one stays there (#1885).
"""

from __future__ import annotations

import ast
from pathlib import Path

OAUTH_ROUTES = Path(__file__).resolve().parents[2] / "src" / "api" / "routes" / "oauth.py"
THREAD_RUNNERS = {"_run_sync"}
# Module-level helpers that deliberately run on asyncio's default pool.
DEFAULT_POOL_HELPERS = {"_run_oauth_sync"}


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
    ``_run_sync(...)`` — never a direct call, never ``asyncio.to_thread``."""
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


def _is_route(node: ast.AST) -> bool:
    """An ``async def`` registered on the router (``@router.get(...)`` etc.)."""
    if not isinstance(node, ast.AsyncFunctionDef):
        return False
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "router"
        ):
            return True
    return False


def _nested_bodies(route: ast.AsyncFunctionDef) -> list[ast.FunctionDef]:
    """Plain ``def``s defined anywhere inside ``route``."""
    return [n for n in ast.walk(route) if isinstance(n, ast.FunctionDef)]


def _awaited_run_sync_argument(node: ast.AST) -> ast.Name | None:
    """The ``body`` name node of ``await _run_sync(body)``, else None."""
    if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    if not (isinstance(call.func, ast.Name) and call.func.id in THREAD_RUNNERS):
        return None
    if len(call.args) != 1 or call.keywords or not isinstance(call.args[0], ast.Name):
        return None
    return call.args[0]


def _nested_body_offenders(tree: ast.Module) -> list[str]:
    """Nested sync bodies of routes that are not run as ``await _run_sync(body)``.

    Every use of the body's name inside its route has to be that one form: a
    direct call runs it on the loop, ``asyncio.to_thread(body)`` /
    ``run_in_executor(None, body)`` put it on the default pool, and a
    ``_run_sync(body)`` that is never awaited does not run it at all.
    """
    offenders: list[str] = []
    for route in (n for n in ast.walk(tree) if _is_route(n)):
        allowed = {
            id(arg)
            for arg in (_awaited_run_sync_argument(n) for n in ast.walk(route))
            if arg is not None
        }
        for body in _nested_bodies(route):
            uses = [
                n
                for n in ast.walk(route)
                if isinstance(n, ast.Name) and n.id == body.name and isinstance(n.ctx, ast.Load)
            ]
            where = f"{route.name}.{body.name} (line {body.lineno})"
            if not uses:
                offenders.append(f"{where}: never run")
            offenders.extend(
                f"{where}: used at line {use.lineno} other than as `await _run_sync({body.name})`"
                for use in uses
                if id(use) not in allowed
            )
    return offenders


def _to_thread_targets(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, first argument) of every ``asyncio.to_thread(...)`` call; the
    argument is '' unless it is a plain name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _runner_name(node) == "to_thread":
            first = node.args[0] if node.args else None
            found.append((node.lineno, first.id if isinstance(first, ast.Name) else ""))
    return found


def _default_pool_offenders(tree: ast.Module) -> list[str]:
    """Ways onto asyncio's default pool other than the allowlisted helpers."""
    module_level = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    offenders = [
        f"line {line}: asyncio.to_thread({name or '<expression>'}) is not allowlisted"
        for line, name in _to_thread_targets(tree)
        if name not in DEFAULT_POOL_HELPERS or name not in module_level
    ]
    # ``loop.run_in_executor(None, ...)`` is the same pool by another name:
    # the one call allowed is the bounded executor inside ``_run_sync``.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _runner_name(node) == "run_in_executor":
            executor = node.args[0] if node.args else None
            if not (isinstance(executor, ast.Name) and executor.id == "_OAUTH_SYNC_EXECUTOR"):
                offenders.append(f"line {node.lineno}: run_in_executor outside the OAuth pool")
    return offenders


def _oauth_tree() -> ast.Module:
    return ast.parse(OAUTH_ROUTES.read_text(encoding="utf-8"))


def test_every_nested_route_body_runs_in_the_bounded_oauth_pool():
    """The sync bodies share one bounded executor (#1831 review): the default
    ``to_thread`` pool could park more threads on a row lock than the sync
    engine has connections."""
    tree = _oauth_tree()
    assert any(_nested_bodies(n) for n in ast.walk(tree) if _is_route(n)), (
        "expected nested sync bodies in the oauth.py routes"
    )
    assert _nested_body_offenders(tree) == []


def test_the_default_pool_is_used_only_by_the_allowlisted_helpers():
    tree = _oauth_tree()
    assert _default_pool_offenders(tree) == []
    # No stale entries: a helper moved onto _run_sync leaves the allowlist too.
    assert {name for _, name in _to_thread_targets(tree)} == DEFAULT_POOL_HELPERS


def test_the_executor_comment_names_every_default_pool_helper():
    lines = OAUTH_ROUTES.read_text(encoding="utf-8").splitlines()
    end = next(i for i, line in enumerate(lines) if line.startswith("_OAUTH_SYNC_EXECUTOR = "))
    # The comment block is the run of ``#`` lines closest above the assignment.
    while end > 0 and not lines[end - 1].startswith("#"):
        end -= 1
    start = end
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    comment = "\n".join(lines[start:end])
    assert "asyncio.to_thread" in comment
    for helper in sorted(DEFAULT_POOL_HELPERS):
        assert helper in comment, f"{helper} is not explained above _OAUTH_SYNC_EXECUTOR"


def _route(source: str) -> ast.Module:
    return ast.parse(
        "@router.post('/x')\nasync def route():\n"
        + "".join(f"    {line}\n" for line in source.splitlines())
    )


def test_the_checks_accept_the_supported_shape():
    tree = _route("def _anything():\n    return 1\nreturn await _run_sync(_anything)")
    assert _nested_body_offenders(tree) == []
    assert _default_pool_offenders(tree) == []


def test_a_nested_body_on_the_default_pool_fails_whatever_its_name():
    for runner in ("asyncio.to_thread(_pre_check)", "to_thread(_work)"):
        name = runner[runner.index("(") + 1 : -1]
        tree = _route(f"def {name}():\n    return 1\nreturn await {runner}")
        assert _nested_body_offenders(tree), runner
        assert _default_pool_offenders(tree), runner


def test_other_ways_past_the_bounded_pool_fail():
    direct = _route("def _sync():\n    return 1\nreturn _sync()")
    assert _nested_body_offenders(direct)
    not_awaited = _route("def _sync():\n    return 1\nreturn _run_sync(_sync)")
    assert _nested_body_offenders(not_awaited)
    unused = _route("def _sync():\n    return 1\nreturn 1")
    assert _nested_body_offenders(unused)
    default_executor = _route(
        "def _sync():\n    return 1\n"
        "return await asyncio.get_running_loop().run_in_executor(None, _sync)"
    )
    assert _nested_body_offenders(default_executor)
    assert _default_pool_offenders(default_executor)
    # A module-level helper that is not on the allowlist.
    stray = ast.parse(
        "def _helper():\n    pass\nasync def f():\n    await asyncio.to_thread(_helper)"
    )
    assert _default_pool_offenders(stray)
    # A lambda or partial hides the target: not allowed either.
    hidden = ast.parse("async def f():\n    await asyncio.to_thread(lambda: 1)")
    assert _default_pool_offenders(hidden)
