"""Guard: every summary-point upsert in ``src/`` carries the BM25 pair (#2053).

``add_memory_to_qdrant`` upserts the WHOLE point. A caller that leaves out
``sparse_indices`` / ``sparse_values`` silently drops the point's ``bm25`` named
vector, and the memory falls out of the sparse leg of hybrid search until it is
next written through the normal path. That shipped in two Sleep paths
(reindex and undo) and nothing in ruff, pyright or the rest of the suite caught
it, because both kwargs are optional.

The checker is a plain function so the negative cases can feed it synthetic
sources, following ``tests/test_stdlib_logger_kwargs_guard.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
# Defines the function (and mentions it in docstrings); not a caller.
SKIP_FILES = frozenset({"db/qdrant.py"})

SPARSE_KWARGS = frozenset({"sparse_indices", "sparse_values"})

# "<path relative to src/>:<function>" for a caller that deliberately
# writes a dense-only point. Empty today — add an entry with a reason.
ALLOWED_DENSE_ONLY: frozenset[str] = frozenset()


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _upsert_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node) == "add_memory_to_qdrant"
    ]


def _passes_sparse_pair(call: ast.Call) -> bool:
    # ``**kwargs`` can't be checked statically (its ``arg`` is None), and a
    # literal ``None`` is a dense-only write spelled out — both count as missing.
    passed = {
        kw.arg
        for kw in call.keywords
        if not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    }
    return SPARSE_KWARGS <= passed


def _source_files() -> list[tuple[str, Path]]:
    files = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel not in SKIP_FILES:
            files.append((rel, path))
    return files


def find_dense_only_upserts(source: str) -> list[tuple[int, str]]:
    """``(line, enclosing function)`` of each call missing the sparse pair."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, func_name: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func_name
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if (
                isinstance(child, ast.Call)
                and _callee_name(child) == "add_memory_to_qdrant"
                and not _passes_sparse_pair(child)
            ):
                found.append((child.lineno, func_name))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_every_upsert_passes_the_sparse_pair() -> None:
    offenders = []
    for rel, path in _source_files():
        for line, func in find_dense_only_upserts(path.read_text(encoding="utf-8")):
            if f"{rel}:{func}" not in ALLOWED_DENSE_ONLY:
                offenders.append(f"src/{rel}:{line} ({func})")
    assert not offenders, (
        "add_memory_to_qdrant() called without sparse_indices/sparse_values — the "
        "upsert drops the point's bm25 vector. Build the point with "
        "build_memory_point(memory) and pass its sparse pair:\n" + "\n".join(offenders)
    )


def test_guard_scans_real_callers() -> None:
    """The guard is not vacuous: it parses the known upsert call sites."""
    calls = [
        call
        for _, path in _source_files()
        for call in _upsert_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]
    # process_pending_embedding, embedding migration, Sleep reindex, Sleep undo.
    assert len(calls) >= 4


def test_guard_flags_a_dense_only_call() -> None:
    src = (
        "async def reindex(m):\n"
        "    await add_memory_to_qdrant(user_id='u', memory_id=m, vector=[0.1],\n"
        "        payload={}, workspace_id='w', context_id='c')\n"
    )
    assert find_dense_only_upserts(src) == [(2, "reindex")]


def test_guard_flags_half_a_pair_kwargs_splat_and_literal_none() -> None:
    src = (
        "async def a(kw):\n"
        "    await qdrant.add_memory_to_qdrant(sparse_indices=[1])\n"
        "    await add_memory_to_qdrant(**kw)\n"
        "    await add_memory_to_qdrant(sparse_indices=None, sparse_values=None)\n"
    )
    assert [line for line, _ in find_dense_only_upserts(src)] == [2, 3, 4]


def test_guard_accepts_the_full_pair() -> None:
    src = (
        "async def ok(m):\n"
        "    p, i, v = build_memory_point(m)\n"
        "    await add_memory_to_qdrant(payload=p, sparse_indices=i, sparse_values=v)\n"
    )
    assert find_dense_only_upserts(src) == []
