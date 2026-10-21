"""Pin VALIDATE CONSTRAINT outside the ALTER's transaction (#2073, #2079).

A migration that adds a constraint ``NOT VALID`` (ACCESS EXCLUSIVE, no scan)
and then runs ``VALIDATE CONSTRAINT`` must not VALIDATE in the same
transaction: ``alembic/env.py`` runs a whole upgrade in one transaction, so the
scan would run while the ALTER's ACCESS EXCLUSIVE lock is still held, blocking
reads and writes. Each VALIDATE runs in ``autocommit_block()``: the NOT VALID
add commits first, and VALIDATE then takes only SHARE UPDATE EXCLUSIVE.

Each migration runs under an offline (``--sql``) PostgreSQL context, so the
captured script carries the ``COMMIT;`` / ``BEGIN;`` alembic emits around an
autocommit block. The offline context's connection is replaced by a recorder,
so the reads some of these migrations make (e84's INVALID-index probe, the
a97 / b03 audits, every downgrade's leftover check) land in the script in
order and find no rows. No database is used.

That recorder is what lets the reads run here; a real ``alembic --sql`` run
has none. ``test_offline_runs_only_without_reads`` pins which revisions need a
live connection (``_NEEDS_LIVE_CONNECTION``).

``test_every_validate_sits_in_an_autocommit_block`` walks every revision's AST.
"""

from __future__ import annotations

import ast
import importlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

_ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_VERSIONS))

# (module, direction) pairs whose run VALIDATEs at least one constraint. The
# b03 / d05 / e01 downgrades drop their constraints and never VALIDATE; the
# a97 downgrade drops its FK too.
_CASES = [
    ("a97_resources_entity", "upgrade"),
    ("b03_396_neural_edges_ws_ctx_not_null", "upgrade"),
    ("d05_523_costgrade_source_paid_by", "upgrade"),
    ("d07_495_cluster_label_phase", "upgrade"),
    ("d07_495_cluster_label_phase", "downgrade"),
    ("e01_546_cache_write_and_pricing_model", "upgrade"),
    ("e74_1401_mae_explore", "upgrade"),
    ("e74_1401_mae_explore", "downgrade"),
    ("e84_1619_tool_guardrails", "upgrade"),
    ("e84_1619_tool_guardrails", "downgrade"),
    ("e98_2007_mae_list_changes_since", "upgrade"),
    ("e98_2007_mae_list_changes_since", "downgrade"),
    ("f00_2019_llm_log_supersede", "upgrade"),
    ("f00_2019_llm_log_supersede", "downgrade"),
]

# Runs that read the database before they finish, so a real ``alembic --sql``
# run cannot carry them, with the result accessor that first fails on the mock
# bind's None: the a97 / b03 upgrade audits, e84's INVALID-index probe, and
# every downgrade's leftover probe.
_NEEDS_LIVE_CONNECTION = {
    ("a97_resources_entity", "upgrade"): "fetchall",
    ("b03_396_neural_edges_ws_ctx_not_null", "upgrade"): "rowcount",
    ("d07_495_cluster_label_phase", "downgrade"): "first",
    ("e74_1401_mae_explore", "downgrade"): "first",
    ("e84_1619_tool_guardrails", "upgrade"): "first",
    ("e84_1619_tool_guardrails", "downgrade"): "first",
    ("e98_2007_mae_list_changes_since", "downgrade"): "first",
    ("f00_2019_llm_log_supersede", "downgrade"): "first",
}

_ADD_NOT_VALID = re.compile(r"ADD CONSTRAINT (\w+)\b.*\bNOT VALID;$")
_VALIDATE = re.compile(r"VALIDATE CONSTRAINT (\w+)\b")
_PHRASE = "VALIDATE CONSTRAINT"

# VALIDATE literals the walker finds across ``alembic/versions``. A new
# migration that VALIDATEs bumps it; a drop means the walker lost a shape.
_KNOWN_SITE_COUNT = 12


class _NoRows:
    """A result with no rows, for each result accessor these migrations use."""

    rowcount = 0

    def first(self) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def scalar(self) -> None:
        return None


class _RecordingBind:
    """Writes each read into the offline script, in order, and returns no rows."""

    def __init__(self, buf: io.StringIO) -> None:
        self._buf = buf

    def execute(self, statement, *_args, **_kwargs) -> _NoRows:
        self._buf.write(f"{statement};\n")
        return _NoRows()


def _offline_script(module: ModuleType, direction: str) -> list[str]:
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf, "transactional_ddl": True},
    )
    # ``op.get_bind()`` returns ``impl.bind`` (the connection, None offline).
    # Offline DDL goes to the buffer, never through it, so only reads see this.
    ctx.impl.connection = _RecordingBind(buf)  # type: ignore[assignment]
    with Operations.context(ctx):
        getattr(module, direction)()
    return [line.strip() for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.parametrize(("revision", "direction"), _CASES)
def test_validate_runs_after_the_not_valid_add_commits(revision, direction):
    """Every VALIDATE follows a COMMIT of its own constraint's NOT VALID add."""
    module = importlib.import_module(revision)
    lines = _offline_script(module, direction)

    validates = [(i, m.group(1)) for i, s in enumerate(lines) if (m := _VALIDATE.search(s))]
    assert validates, lines
    commits = [i for i, s in enumerate(lines) if s == "COMMIT;"]
    begins = [i for i, s in enumerate(lines) if s == "BEGIN;"]

    for validate, name in validates:
        add = max(
            i
            for i, s in enumerate(lines[:validate])
            if (m := _ADD_NOT_VALID.search(s)) and m.group(1) == name
        )
        # A COMMIT separates the ACCESS EXCLUSIVE add from the scan ...
        assert any(add < c < validate for c in commits), (name, lines)
        # ... and VALIDATE sits inside the autocommit block (before its BEGIN).
        opened = max(c for c in commits if c < validate)
        assert any(b > validate for b in begins), (name, lines)
        assert not any(opened < b < validate for b in begins), (name, lines)


def _is_autocommit_block(item: ast.withitem) -> bool:
    expr = item.context_expr
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "autocommit_block"
    )


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            piece.value
            for piece in node.values
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    return None


def _validate_sites(tree: ast.AST) -> list[tuple[int, bool]]:
    """``(lineno, inside an autocommit block)`` for every VALIDATE literal
    that is not a docstring: call arguments, assigned values, returns. A
    docstring (or any bare string statement) is an ``ast.Expr`` whose value
    is the literal; comments are not in the AST at all."""
    sites: list[tuple[int, bool]] = []

    def visit(node: ast.AST, in_block: bool, parent: ast.AST | None) -> None:
        if isinstance(node, ast.With) and any(_is_autocommit_block(i) for i in node.items):
            in_block = True
        text = _literal_text(node)
        if text is not None:
            if _PHRASE in text and not isinstance(parent, ast.Expr):
                sites.append((getattr(node, "lineno", 0), in_block))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_block, node)

    visit(tree, False, None)
    return sites


def _all_validate_sites() -> dict[str, list[tuple[int, bool]]]:
    return {
        path.name: sites
        for path in sorted(_ALEMBIC_VERSIONS.glob("*.py"))
        if (sites := _validate_sites(ast.parse(path.read_text(), filename=str(path))))
    }


def test_every_validate_sits_in_an_autocommit_block():
    """Every VALIDATE literal in ``alembic/versions`` is lexically inside
    ``with op.get_context().autocommit_block():`` — the ``_validate()`` helper
    form passes because its literal sits inside the ``with``.

    A VALIDATE held in a variable counts where the literal is written: inside
    the ``with`` it passes, outside it is an offender. So a module constant
    (``_DDL = "... VALIDATE ..."``) executed inside a block is reported as
    outside (a known false positive, the safe direction). No migration does
    that; write the literal inside the block instead.
    """
    sites = _all_validate_sites()
    offenders = {
        name: [line for line, in_block in found if not in_block]
        for name, found in sites.items()
        if not all(in_block for _, in_block in found)
    }
    assert offenders == {}


def test_the_validate_walker_finds_the_known_sites():
    """Positive guard, so a gap in the walker cannot pass the check above."""
    sites = _all_validate_sites()
    for name in (
        "a97_resources_entity.py",
        "b03_396_neural_edges_ws_ctx_not_null.py",
        "d05_523_costgrade_source_paid_by.py",
        "d07_495_cluster_label_phase.py",
        "e01_546_cache_write_and_pricing_model.py",
        "e65_1275_api_keys_agent.py",
        "e67_1281_agent_key_workspace.py",
        "f00_2019_llm_log_supersede.py",
    ):
        assert name in sites, sorted(sites)
    assert sum(len(found) for found in sites.values()) == _KNOWN_SITE_COUNT, sites


@pytest.mark.parametrize(("revision", "direction"), _CASES)
def test_offline_runs_only_without_reads(revision, direction):
    """What the ``_validate()`` docstrings claim for a real ``alembic --sql`` run:
    no recorder, so a read of the database fails on the mock bind."""
    module = importlib.import_module(revision)
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": io.StringIO(), "transactional_ddl": True},
    )
    with Operations.context(ctx):
        accessor = _NEEDS_LIVE_CONNECTION.get((revision, direction))
        if accessor is None:
            getattr(module, direction)()
        else:
            # The mock connection returns None for a read; its accessor fails.
            with pytest.raises(
                AttributeError, match=f"'NoneType' object has no attribute '{accessor}'"
            ):
                getattr(module, direction)()
