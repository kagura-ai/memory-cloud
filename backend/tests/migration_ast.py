"""AST walker for the migration lint tests (#2079, #2240).

Every check reads ``alembic/versions`` without importing it and reports each
site as ``(lineno, inside an autocommit block)``. "Inside" is lexical: the
site is in the body of ``with op.get_context().autocommit_block():`` (any
``with`` whose item calls an ``autocommit_block`` attribute), or in a later
item of the same ``with``. A helper function defined outside the block and
called inside it is reported as outside, and so is a local variable assigned
before the block and read inside it: for the checks that want a site inside,
the safe direction.

A docstring (any bare string statement) is never a site; comments are not in
the AST at all.
"""

from __future__ import annotations

import ast
import io
from collections.abc import Callable, Iterable
from functools import cache
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

Site = tuple[int, bool]


def is_autocommit_block(item: ast.withitem) -> bool:
    expr = item.context_expr
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "autocommit_block"
    )


def literal_text(node: ast.AST) -> str | None:
    """A string literal's text; an f-string's literal parts, joined."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            piece.value
            for piece in node.values
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    return None


def _walk(tree: ast.AST, report: Callable[[ast.AST, bool, ast.AST | None], bool]) -> None:
    """Call ``report(node, in_block, parent)`` on every node; a True return
    stops the descent below ``node``."""

    def visit(node: ast.AST, in_block: bool, parent: ast.AST | None) -> None:
        if report(node, in_block, parent):
            return
        if isinstance(node, ast.With):
            # Items enter in order: one before the autocommit_block item runs
            # in the transaction, one after it inside the block.
            for item in node.items:
                visit(item, in_block, node)
                in_block = in_block or is_autocommit_block(item)
            for stmt in node.body:
                visit(stmt, in_block, node)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_block, node)

    visit(tree, False, None)


def literal_sites(
    tree: ast.AST, matches: Callable[[str], bool], *, follow_constants: bool = False
) -> list[Site]:
    """Every string literal whose text ``matches``, docstrings excepted.

    With ``follow_constants``, a module-level constant holding such a literal
    (``_INDEX_DDL = "CREATE INDEX CONCURRENTLY ..."``) counts where the
    constant is read, not where it is written, so a revision that executes its
    constant inside the block passes. A constant built from another one
    counts the same way.
    """
    constants: set[str] = set()
    skipped: set[int] = set()
    if follow_constants and isinstance(tree, ast.Module):
        for stmt in tree.body:
            target, value = _constant_assignment(stmt)
            if target is None or value is None:
                continue
            if _holds_match(value, matches, constants):
                constants.add(target)
                skipped.add(id(stmt))

    sites: list[Site] = []

    def report(node: ast.AST, in_block: bool, parent: ast.AST | None) -> bool:
        if id(node) in skipped:
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in constants:
            sites.append((node.lineno, in_block))
            return True
        if isinstance(parent, ast.JoinedStr):
            # An f-string's literal parts were read with the f-string.
            return isinstance(node, ast.Constant)
        text = literal_text(node)
        if text is None:
            return False
        if matches(text) and not isinstance(parent, ast.Expr):
            sites.append((getattr(node, "lineno", 0), in_block))
        # Descend into an f-string's ``{...}`` parts: a constant read there.
        return not isinstance(node, ast.JoinedStr)

    _walk(tree, report)
    return sites


def _constant_assignment(stmt: ast.stmt) -> tuple[str | None, ast.expr | None]:
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id, stmt.value
    return None, None


def _holds_match(value: ast.expr, matches: Callable[[str], bool], constants: set[str]) -> bool:
    for node in ast.walk(value):
        if isinstance(node, ast.Name) and node.id in constants:
            return True
        text = literal_text(node)
        if text is not None and matches(text):
            return True
    return False


def call_sites(tree: ast.AST, names: Iterable[str]) -> list[Site]:
    """Every call of a function named in ``names`` (``f(...)`` or ``m.f(...)``)."""
    wanted = set(names)
    sites: list[Site] = []

    def report(node: ast.AST, in_block: bool, _parent: ast.AST | None) -> bool:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in wanted:
                sites.append((node.lineno, in_block))
        return False

    _walk(tree, report)
    return sites


def keyword_sites(tree: ast.AST, arg: str) -> list[Site]:
    """Every call that passes keyword ``arg`` a value other than literal False/None."""
    sites: list[Site] = []

    def report(node: ast.AST, in_block: bool, _parent: ast.AST | None) -> bool:
        if isinstance(node, ast.keyword) and node.arg == arg:
            value = node.value
            if not (isinstance(value, ast.Constant) and value.value in (False, None)):
                sites.append((value.lineno, in_block))
        return False

    _walk(tree, report)
    return sites


def revision_id(tree: ast.Module) -> str | None:
    """The module's ``revision`` string."""
    for stmt in tree.body:
        target, value = _constant_assignment(stmt)
        if (
            target == "revision"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


@cache
def migration_trees() -> dict[str, ast.Module]:
    """Each revision file's AST, by file name (read once per run; do not
    mutate the result)."""
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(ALEMBIC_VERSIONS.glob("*.py"))
    }


def offline_sql(
    fn: Callable[[], object], bind: Callable[[io.StringIO], object] | None = None
) -> list[str]:
    """The offline (``--sql``, transactional) PostgreSQL script ``fn`` emits,
    one stripped line per entry.

    ``op.get_bind()`` is the offline connection (None) unless ``bind`` makes
    one from the output buffer: offline DDL goes to the buffer, never through
    it, so only reads see it.
    """
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf, "transactional_ddl": True},
    )
    if bind is not None:
        ctx.impl.connection = bind(buf)  # type: ignore[assignment]
    with Operations.context(ctx):
        fn()
    return [line.strip() for line in buf.getvalue().splitlines() if line.strip()]
