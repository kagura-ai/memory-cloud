"""Guard: every Qdrant scroll in ``backend/src`` sees summary points only (#2041).

#2015 added content chunk points (payload ``chunk_of``) to the memory
collections. A scroll that assumes one point per memory — context recovery,
the IDF snapshot, the geo reconcile, the embedding-migration verify — must
leave them out, and new code can easily forget to. So every ``.scroll(`` call
must pass ``scroll_filter=`` built by ``db.qdrant.summary_points_filter``:
either the call itself, or a local name assigned from it in the same function.

A scroll that legitimately wants chunk points goes on ``ALLOWED`` with the
reason. An entry that no longer names a scroll fails, so the list stays honest.

Scope: ``.scroll(`` calls only. A summary-only read written as ``retrieve`` (by
id) or ``query_points`` is not checked here; dense searches exclude chunks in
``search_memories_qdrant`` (``include_chunks=False``).

Follows ``tests/test_stdlib_logger_kwargs_guard.py``: the checker is a plain
function, so synthetic sources prove it both catches the bug class and stays
quiet on the safe patterns.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

HELPER = "summary_points_filter"

# "<path relative to src>::<enclosing function>" -> why it may see chunk points.
ALLOWED: dict[str, str] = {
    "db/qdrant.py::scroll_point_refs": (
        "orphan sweep: judges every point, chunk points included, by its memory row"
    ),
    "db/qdrant.py::count_context_chunk_points": (
        "migration verify counts chunk points per memory; it wants chunk points only"
    ),
}


def _is_helper_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == HELPER) or (
        isinstance(func, ast.Attribute) and func.attr == HELPER
    )


def _stored_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _helper_bound_names(func: ast.AST) -> set[str]:
    """Names every assignment of which inside ``func`` is ``summary_points_filter(...)``.

    A name that is also bound to anything else in the function (a later
    reassignment, an augmented assignment, a loop target, a parameter) does not
    count: the scroll might see that other value.
    """
    helper: set[str] = set()
    other: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            bucket = helper if _is_helper_call(node.value) else other
            for target in node.targets:
                bucket.update(_stored_names(target))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            (helper if _is_helper_call(node.value) else other).add(node.target.id)
        elif isinstance(node, ast.AugAssign | ast.For | ast.AsyncFor):
            other.update(_stored_names(node.target))
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    other.update(_stored_names(item.optional_vars))
        elif isinstance(node, ast.NamedExpr):
            other.add(node.target.id)
        elif isinstance(node, ast.arg):
            other.add(node.arg)
    return helper - other


def _scroll_calls(tree: ast.Module) -> list[tuple[str, int, ast.Call, ast.AST]]:
    """``(enclosing function name, line, call, function node)`` per ``.scroll(``."""
    found: list[tuple[str, int, ast.Call, ast.AST]] = []

    def _visit(node: ast.AST, func: ast.AST | None, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                _visit(child, child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "scroll"
            ):
                found.append((name, child.lineno, child, func if func is not None else tree))
            _visit(child, func, name)

    _visit(tree, None, "<module>")
    return found


def find_unfiltered_scrolls(source: str, rel_path: str) -> tuple[list[str], set[str]]:
    """Return (violations, allow-list keys seen) for one module's source."""
    tree = ast.parse(source)
    violations: list[str] = []
    seen: set[str] = set()
    for func_name, line, call, func in _scroll_calls(tree):
        key = f"{rel_path}::{func_name}"
        if key in ALLOWED:
            seen.add(key)
            continue
        flt = next((kw.value for kw in call.keywords if kw.arg == "scroll_filter"), None)
        if _is_helper_call(flt):
            continue
        if isinstance(flt, ast.Name) and flt.id in _helper_bound_names(func):
            continue
        violations.append(f"{rel_path}:{line} ({func_name})")
    return violations, seen


def test_every_scroll_in_src_goes_through_the_helper() -> None:
    violations: list[str] = []
    seen: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        found, allowed = find_unfiltered_scrolls(path.read_text(encoding="utf-8"), rel)
        violations.extend(found)
        seen |= allowed
    assert not violations, (
        "Qdrant scroll without summary_points_filter (would also see content "
        "chunk points, #2015). Build scroll_filter with db.qdrant."
        "summary_points_filter(...), or add the function to ALLOWED with a "
        "reason:\n  " + "\n  ".join(violations)
    )
    stale = set(ALLOWED) - seen
    assert not stale, f"ALLOWED names no scroll any more: {sorted(stale)}"


# --------------------------------------------------------------- the checker


def _check(source: str) -> list[str]:
    return find_unfiltered_scrolls(source, "m.py")[0]


def test_flags_a_scroll_without_a_filter() -> None:
    src = "async def f(c):\n    await c.scroll(collection_name='x')\n"
    assert _check(src) == ["m.py:2 (f)"]


def test_flags_a_hand_built_filter() -> None:
    src = (
        "async def f(c):\n"
        "    flt = Filter(must=[cond])\n"
        "    await c.scroll(collection_name='x', scroll_filter=flt)\n"
    )
    assert _check(src) == ["m.py:3 (f)"]


def test_flags_a_helper_name_bound_in_another_function() -> None:
    src = (
        "def g():\n"
        "    flt = summary_points_filter()\n"
        "async def f(c, flt):\n"
        "    await c.scroll(scroll_filter=flt)\n"
    )
    assert _check(src) == ["m.py:4 (f)"]


def test_flags_a_helper_name_reassigned_to_something_else() -> None:
    src = (
        "async def f(c, x):\n"
        "    flt = summary_points_filter(a)\n"
        "    if x:\n"
        "        flt = Filter(must=[a])\n"
        "    await c.scroll(scroll_filter=flt)\n"
    )
    assert _check(src) == ["m.py:5 (f)"]


def test_flags_a_helper_name_rebound_by_a_loop() -> None:
    src = (
        "async def f(c, filters):\n"
        "    flt = summary_points_filter(a)\n"
        "    for flt in filters:\n"
        "        await c.scroll(scroll_filter=flt)\n"
    )
    assert _check(src) == ["m.py:4 (f)"]


def test_accepts_the_helper_inline_and_through_a_local_name() -> None:
    src = (
        "async def f(c):\n"
        "    await c.scroll(scroll_filter=summary_points_filter(a))\n"
        "async def g(c):\n"
        "    flt = qdrant.summary_points_filter(a, must_not=[b])\n"
        "    await c.scroll(scroll_filter=flt)\n"
        "async def h(c):\n"
        "    flt: Filter = summary_points_filter()\n"
        "    await c.scroll(scroll_filter=flt)\n"
        "async def k(c, names):\n"
        "    for name in names:\n"
        "        with lock:\n"
        "            flt = summary_points_filter(a)\n"
        "            await c.scroll(collection_name=name, scroll_filter=flt)\n"
    )
    assert _check(src) == []


def test_allow_listed_function_is_reported_as_seen() -> None:
    src = "async def scroll_point_refs(c):\n    await c.scroll(collection_name='x')\n"
    violations, seen = find_unfiltered_scrolls(src, "db/qdrant.py")
    assert violations == []
    assert seen == {"db/qdrant.py::scroll_point_refs"}
