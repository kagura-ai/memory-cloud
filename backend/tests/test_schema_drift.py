"""ORM ``__table_args__`` CHECK constraint vs alembic migration drift detector.

Caught at PR #568 review loop 1: ``extra_sleep_contexts`` was added to the
``WorkspaceAddon.__table_args__`` ``CheckConstraint`` string AND to a new
alembic migration, but earlier work paths that used
``Base.metadata.create_all(...)`` instead of running migrations would
silently use a stale ORM CHECK string and reject the new addon type. Issue
#587 (deferred from #570 acceptance) asks for a CI-level lint covering this
class of bug for every named ``CheckConstraint`` in the ORM.

The detector:

1. Enumerates every named :class:`~sqlalchemy.schema.CheckConstraint` on
   ``Base.metadata`` (across all imported model modules).
2. Walks ``backend/alembic/versions/*.py`` in **alembic dependency order**
   (a Kahn-style topological walk over the ``revision`` /
   ``down_revision`` graph extracted via AST — see
   :func:`_migrations_in_dependency_order`). Filename sort is **not** used,
   because lexicographic order disagrees with the revision chain in this
   repo (``a120_*`` has ``down_revision="a90_*"`` yet sorts before
   ``a51_*``). For each file it parses the AST of ``upgrade()`` (no code
   execution — :func:`ast.parse` only) and extracts CHECK clauses from any
   of the four call shapes used in this project:

      a. ``sa.CheckConstraint("…", name="…")`` inside ``op.create_table(…)``
      b. ``op.create_check_constraint(name, table, condition)``
      c. ``op.execute("ALTER TABLE … ADD CONSTRAINT … CHECK (…)")``
      d. ``op.execute(sa.text("ALTER TABLE … ADD CONSTRAINT … CHECK (…)"))``

3. Asserts the ORM and the latest-migration SQL match after whitespace
   normalization. "Latest" is determined by alembic dependency order
   (the migration whose ``revision`` is reachable last from the root via
   the ``down_revision`` chain), not by filename sort.

**Constant resolution**: only string-literal AST nodes (and module-level
constants assigned directly from such literals) are resolved. Constants
computed via function calls (e.g. ``_FOO = _build_sql(_TYPES)``) are not
resolved — running such code would require executing the migration module,
which we avoid for safety. In the current codebase every constraint's
*latest* migration uses literal SQL, so this limitation does not affect
detection. If a future migration introduces a function-computed constant as
the latest definition of a constraint, this test will fail with "ORM
defines CHECK '…' but no migration creates it", pointing the implementer
at the resolver gap.

Acceptance for #587 mapped to this file:

- "CI step on every PR" → runs in the ``backend-unit`` job (``pytest`` picks
  this file up because it lives at ``backend/tests/test_schema_drift.py``,
  not under ``tests/integration`` which CI excludes).
- "Step fails on disagreement" → ``assert orm_sql == migration_sql``.
- "Documented escape hatch" → wrap the offending case in
  ``pytest.param(<table>, <constraint>, <orm_sql>,
  marks=pytest.mark.skip(reason="known drift, follow-up #N"),
  id="<table>.<constraint>")`` inside the parametrize call (or, for a
  conditional skip, ``pytest.skip(reason="…")`` inside the test body
  itself). The ``marks=`` form is the only way to attach a skip to a
  specific parametrize case. There is no in-source pragma on the
  ``CheckConstraint`` — we want skips to surface in test reports.
- "WorkspaceAddon.addon_type covered" → covered by the parametrized loop AND
  by a named regression test below.
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint as SACheckConstraint

from db.base import Base

# Side-effect "imports": ensure every model module registers its tables with
# ``Base.metadata`` before we enumerate them. ``conftest.py`` already imports
# a subset (auth/memory/llm_pricing/sleep/analysis); the list below is the
# full set so this file is robust to conftest changes and to running it via
# ``pytest backend/tests/test_schema_drift.py`` in isolation.
#
# Done via ``importlib.import_module`` rather than ``import models.X`` so
# static analyzers (``F401``-aware linters like ruff respect ``# noqa: F401``,
# but the github-code-quality "unused import" check does not) don't flag
# each line — the imports ARE used (their import-time side effect populates
# ``Base.metadata``) but no attribute on the imported module is dereferenced
# by name in this file.
_MODEL_MODULES: tuple[str, ...] = (
    "models.analysis",
    "models.auth",
    "models.bm25_drift",
    "models.config",
    "models.erasure",
    "models.file_objects",
    "models.hub_tag",
    "models.llm_pricing",
    "models.memory",
    "models.neural",
    "models.referral",
    "models.resource",
    "models.signup_gate",
    "models.sleep",
)
for _model_module_name in _MODEL_MODULES:
    importlib.import_module(_model_module_name)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Match ``ALTER TABLE <table>`` so we can pair each subsequent
# ``ADD CONSTRAINT … CHECK (…)`` with the most recently named table
# (handles the compound ``ALTER TABLE t DROP CONSTRAINT …, ADD CONSTRAINT
# … CHECK (…)`` form used in the zero-downtime migration pattern).
_ALTER_TABLE_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)", re.IGNORECASE)
_ADD_CONSTRAINT_CHECK_RE = re.compile(
    r"ADD\s+CONSTRAINT\s+(\w+)\s+CHECK\s*\(",
    re.IGNORECASE,
)
# Match ``<col> IN (<literals>)`` so we can sort the literal list and
# absorb harmless reorderings (an unordered IN list is set-equal regardless
# of textual order). Anchored to the whole CHECK so we do not partially
# rewrite compound clauses like ``a IN (…) AND b > 0``. The ``[^()]+`` body
# (rather than non-greedy ``.+?``) is load-bearing: with ``.+?`` and the
# ``\Z`` end-anchor the regex would over-match a compound CHECK with two
# IN-lists (``a IN ('x') AND b IN ('y')``) by greedily consuming through
# the inner ``)``, mangling the literal list. ``[^()]+`` constrains the
# match to plain IN-lists with no nested parens, so compound clauses fall
# through to whitespace-only normalization untouched.
_IN_LIST_RE = re.compile(
    r"\A\s*(\S+)\s+IN\s*\(\s*([^()]+?)\s*\)\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_whitespace(sql: str) -> str:
    """Collapse whitespace and strip trailing punctuation."""
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";").strip()


def _normalize(sql: str) -> str:
    """Whitespace-normalize, then apply IN-list literal sorting if the
    CHECK is the simple ``<col> IN (<literals>)`` shape. Compound CHECKs
    fall through unchanged."""
    sql = _normalize_whitespace(sql)
    match = _IN_LIST_RE.match(sql)
    if not match:
        return sql
    col, literals = match.group(1), match.group(2)
    items = sorted(item.strip() for item in literals.split(","))
    return f"{col} IN ({', '.join(items)})"


def _string_from_ast(node: ast.AST | None, constants: dict[str, str] | None = None) -> str | None:
    """Return the ``str`` value of an AST node iff the node is a string
    constant, an implicit-concatenation chain of constants, or an f-string
    whose interpolated parts are simple ``{NAME}`` references to known
    string constants. Returns ``None`` for anything that would require
    code execution (function calls, expressions inside ``{…}``, etc.)."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
                continue
            if (
                isinstance(piece, ast.FormattedValue)
                and constants is not None
                and isinstance(piece.value, ast.Name)
                and piece.value.id in constants
            ):
                parts.append(constants[piece.value.id])
                continue
            return None
        return "".join(parts)
    return None


def _find_check_clauses_in_sql(sql: str) -> list[tuple[str, str, str]]:
    """Scan ``sql`` for every ``ALTER TABLE <t> … ADD CONSTRAINT <n> CHECK
    (<inner>) [NOT VALID]`` shape and return ``(table, name, inner)`` per
    clause. Pairs each ADD with the most recent preceding ALTER TABLE so
    compound ``ALTER TABLE t DROP …, ADD …`` statements work correctly."""
    table_positions = [(m.start(), m.group(1)) for m in _ALTER_TABLE_RE.finditer(sql)]
    found: list[tuple[str, str, str]] = []
    for add_match in _ADD_CONSTRAINT_CHECK_RE.finditer(sql):
        table: str | None = None
        for pos, tname in table_positions:
            if pos < add_match.start():
                table = tname
            else:
                break
        if not table:
            continue
        # Walk balanced parens starting just after the regex-consumed ``(``.
        i = add_match.end()
        depth = 1
        while i < len(sql) and depth > 0:
            ch = sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        inner = sql[add_match.end() : i - 1]
        found.append((table, add_match.group(1), inner))
    return found


class _MigrationCheckExtractor:
    """AST-only walker. Discovers CHECK constraint definitions inside a
    migration's ``upgrade()`` function without executing any code.

    Attributes
    ----------
    checks
        ``[(table_name, constraint_name, normalized_sql), …]`` in source
        order. The caller decides ordering across files.
    """

    def __init__(self, source: str, path: Path) -> None:
        self.path = path
        self.checks: list[tuple[str, str, str]] = []
        try:
            self._tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover — migrations should always parse
            raise AssertionError(f"Migration {path.name} failed to parse: {exc}") from exc
        self._constants: dict[str, str] = {}
        self._collect_module_constants()
        self._scan_upgrade()

    def _collect_module_constants(self) -> None:
        """Record every module-level ``NAME = literal_string`` assignment.

        Function-call right-hand sides are deliberately skipped — see the
        module docstring for the rationale.
        """
        # Iterate body in source order so an f-string constant can resolve
        # interpolations against earlier-defined constants in the same file
        # (e.g. ``_OLD = "..."`` then ``_NEW = f"... {_OLD} ..."``).
        for node in self._tree.body:
            target_node: ast.Name | None = None
            value_node: ast.AST | None = None
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                target_node = node.targets[0]
                value_node = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                target_node = node.target
                value_node = node.value
            if target_node is None or value_node is None:
                continue
            val = _string_from_ast(value_node, self._constants)
            if val is not None:
                self._constants[target_node.id] = val

    def _resolve_str(self, node: ast.AST) -> str | None:
        """Resolve an AST node to a Python ``str`` if possible.

        Handles three shapes:

        1. ``"literal"`` (``ast.Constant``)
        2. ``CONST_NAME`` referring to a module-level string constant
        3. ``sa.text("literal")`` — unwrap the wrapper
        """
        direct = _string_from_ast(node, self._constants)
        if direct is not None:
            return direct
        if isinstance(node, ast.Name):
            return self._constants.get(node.id)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "text" and node.args:
                return self._resolve_str(node.args[0])
        return None

    def _resolve_call_arg(self, call: ast.Call, position: int, kwarg_name: str) -> str | None:
        """Resolve a string-valued argument either positionally or by
        keyword. Alembic's ``op.create_check_constraint`` accepts
        ``(constraint_name, table_name, condition)`` either way; without
        kwarg fallback the detector silently skips kwarg-style migrations
        and leaves a hole in coverage."""
        if position < len(call.args):
            return self._resolve_str(call.args[position])
        for kw in call.keywords:
            if kw.arg == kwarg_name:
                return self._resolve_str(kw.value)
        return None

    def _scan_upgrade(self) -> None:
        upgrade = next(
            (n for n in self._tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"),
            None,
        )
        if upgrade is None:
            return
        # DFS preorder, NOT ast.walk: ast.walk is breadth-first by depth,
        # which yields a sibling top-level call BEFORE a source-earlier call
        # nested inside an if/with/for body. _build_latest_migration_check_map
        # relies on iteration order for "last definition wins" semantics, so
        # a migration that DROP+ADD-CONSTRAINTs the same CHECK across a
        # control-flow boundary would be resolved incorrectly with BFS.
        for call in self._iter_calls_preorder(upgrade):
            self._process_call(call)

    @staticmethod
    def _iter_calls_preorder(node: ast.AST) -> Iterator[ast.Call]:
        """Yield every ``Call`` descended from ``node`` in DFS preorder
        (source order). Replacement for ``ast.walk`` — see ``_scan_upgrade``."""
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            yield from _MigrationCheckExtractor._iter_calls_preorder(child)

    def _process_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if not (isinstance(func.value, ast.Name) and func.value.id == "op"):
            return
        method = func.attr

        if method == "execute" and node.args:
            sql = self._resolve_str(node.args[0])
            if sql:
                # A single ``op.execute(...)`` may contain multiple CHECK
                # additions (the compound ``DROP …, ADD …`` form), so iterate
                # all matches rather than taking the first.
                for table, name, inner in _find_check_clauses_in_sql(sql):
                    self.checks.append((table, name, _normalize(inner)))

        elif method == "create_check_constraint":
            # Each of (constraint_name, table_name, condition) may be passed
            # positionally or by keyword. Currently every project migration
            # uses positional, but supporting kwargs prevents a future
            # contributor's switch to keyword form from silently dropping
            # coverage.
            name = self._resolve_call_arg(node, 0, "constraint_name")
            table = self._resolve_call_arg(node, 1, "table_name")
            cond = self._resolve_call_arg(node, 2, "condition")
            if name and table and cond:
                self.checks.append((table, name, _normalize(cond)))

        elif method == "create_table" and node.args:
            table = self._resolve_str(node.args[0])
            if not table:
                return
            for arg in node.args[1:]:
                pair = self._extract_sa_check_constraint(arg)
                if pair is not None:
                    self.checks.append((table, pair[0], _normalize(pair[1])))

    def _extract_sa_check_constraint(self, node: ast.AST) -> tuple[str, str] | None:
        """If ``node`` is ``sa.CheckConstraint("…", name="…")`` return
        ``(name, sql)``. Returns ``None`` for any other shape."""
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "CheckConstraint"):
            return None
        if not node.args:
            return None
        sql = self._resolve_str(node.args[0])
        if sql is None:
            return None
        name: str | None = None
        for kw in node.keywords:
            if kw.arg == "name":
                name = self._resolve_str(kw.value)
                break
        if name is None:
            return None
        return (name, sql)


def _orm_check_sqltext_raw(constraint: SACheckConstraint) -> str:
    """Read a CheckConstraint's SQL as the **raw string** the model file
    declared, NOT the dialect-compiled form.

    For constraints constructed with a string literal (the only form this
    codebase uses), ``constraint.sqltext`` is a :class:`sqlalchemy.sql.elements.TextClause`
    whose ``.text`` attribute is exactly that literal — stable across
    SQLAlchemy versions and dialects. ``str(constraint.sqltext)`` instead
    routes through SQLAlchemy's expression compiler, which can rewrite
    quoting, spacing, or operator forms across major SA upgrades and
    produce false-positive drift failures.

    Falls back to ``str(...)`` for the rare case of a programmatically
    constructed ColumnElement (no ``.text`` attribute). Currently zero
    project CHECKs use that form; the fallback exists so a future addition
    doesn't trip the detector at import time.
    """
    raw = getattr(constraint.sqltext, "text", None)
    if isinstance(raw, str):
        return raw
    return str(constraint.sqltext)


def _enumerate_orm_check_constraints() -> list[tuple[str, str, str]]:
    """Return every named ``CheckConstraint`` on ``Base.metadata`` as
    ``(table_name, constraint_name, normalized_sql)`` tuples, fully sorted
    for deterministic test ordering.

    Both axes are sorted explicitly: tables by ``name``, and within each
    table, constraints by ``name``. ``Table.constraints`` is a set-like
    collection in SQLAlchemy — its iteration order depends on hash seed
    and SQLAlchemy internals, so without the inner sort the parametrize
    ids would vary between runs (and CI re-runs could shuffle, masking
    flakes).
    """
    seen: set[tuple[str, str]] = set()
    rows: list[tuple[str, str, str]] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        # Build (constraint, name_str) pairs so the sort key is a guaranteed
        # str — SQLAlchemy types `Constraint.name` as `str | _NoneName`, and
        # `_NoneName` does not implement `__lt__`, so passing the attribute
        # directly to `sorted` trips a pyright reportArgumentType.
        named_checks: list[tuple[SACheckConstraint, str]] = [
            (c, c.name)
            for c in table.constraints
            if isinstance(c, SACheckConstraint) and isinstance(c.name, str) and c.name
        ]
        for constraint, name in sorted(named_checks, key=lambda pair: pair[1]):
            key = (table.name, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    table.name,
                    name,
                    _normalize(_orm_check_sqltext_raw(constraint)),
                )
            )
    return rows


def _migration_revision_metadata(path: Path) -> tuple[str | None, list[str]]:
    """AST-parse a migration file and extract ``(revision, [down_revision_ids])``.

    Returns ``(None, [])`` if the file cannot be parsed or its revision
    metadata is absent. ``down_revision`` may be ``None`` (root), a single
    string (linear chain), or a tuple of strings (a merge migration); all
    three forms are flattened into a list for uniform graph construction.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return (None, [])
    rev: str | None = None
    down_ids: list[str] = []
    for node in tree.body:
        target_name: str | None = None
        value_node: ast.AST | None = None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target_name = node.target.id
            value_node = node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target_name = node.targets[0].id
            value_node = node.value
        if target_name is None or value_node is None:
            continue
        if target_name == "revision":
            rev = _string_from_ast(value_node)
        elif target_name == "down_revision":
            if isinstance(value_node, ast.Constant) and value_node.value is None:
                down_ids = []
            elif isinstance(value_node, ast.Tuple):
                for elt in value_node.elts:
                    s = _string_from_ast(elt)
                    if s:
                        down_ids.append(s)
            else:
                s = _string_from_ast(value_node)
                if s:
                    down_ids = [s]
    return (rev, down_ids)


def _migrations_in_dependency_order() -> list[Path]:
    """Return migration file paths in alembic dependency order (oldest first).

    Filename-sort is **not** a substitute for dependency order: alembic's
    ``down_revision`` chain can — and in this repo does — disagree with
    filename sort (``a120_*`` has ``down_revision="a90_*"`` yet sorts
    before ``a51_*`` lexicographically). Picking the wrong "latest"
    definition would silently mismatch the production schema.

    Builds a forward graph (``down_revision → [revisions]``) by AST-parsing
    each migration's revision metadata, then performs a Kahn-style
    topological walk from the root(s). Migrations whose metadata can't be
    parsed are appended at the end (filename-sorted) so they still get
    scanned even if their position is unknown.
    """
    rev_to_path: dict[str, Path] = {}
    rev_to_downs: dict[str, list[str]] = {}
    unparseable: list[Path] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        rev, downs = _migration_revision_metadata(path)
        if rev is None:
            unparseable.append(path)
            continue
        rev_to_path[rev] = path
        rev_to_downs[rev] = downs

    # Forward graph: down_revision → [revisions that point to it]. Roots use
    # the ``None`` key (down_revision is empty list).
    forward: dict[str | None, list[str]] = {}
    indegree: dict[str, int] = {}
    for rev, downs in rev_to_downs.items():
        indegree[rev] = len(downs)
        if not downs:
            forward.setdefault(None, []).append(rev)
        else:
            for d in downs:
                forward.setdefault(d, []).append(rev)

    # Kahn's algorithm: start from indegree-0 (roots).
    ordered: list[Path] = []
    queue: list[str] = [r for r, d in indegree.items() if d == 0]
    queue.sort()  # deterministic root ordering when there are multiple
    while queue:
        rev = queue.pop(0)
        ordered.append(rev_to_path[rev])
        for child in sorted(forward.get(rev, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    # Any leftover revisions (cycles or unreachable) — shouldn't happen with
    # a valid alembic chain, but append deterministically so we don't drop
    # CHECK definitions silently.
    seen_paths = set(ordered)
    for path in rev_to_path.values():
        if path not in seen_paths:
            ordered.append(path)
            seen_paths.add(path)
    for path in unparseable:
        if path not in seen_paths:
            ordered.append(path)
    return ordered


def _build_latest_migration_check_map() -> dict[tuple[str, str], str]:
    """Walk migrations in alembic dependency order; the last definition for
    each ``(table, name)`` wins."""
    latest: dict[tuple[str, str], str] = {}
    for path in _migrations_in_dependency_order():
        extractor = _MigrationCheckExtractor(path.read_text(encoding="utf-8"), path)
        for table, name, sql in extractor.checks:
            latest[(table, name)] = sql
    return latest


# Computed once at module import. ~14 model modules + ~30 migrations: cheap.
_ORM_CHECKS: list[tuple[str, str, str]] = _enumerate_orm_check_constraints()
_MIGRATION_CHECKS: dict[tuple[str, str], str] = _build_latest_migration_check_map()


# Each case is a ``pytest.param`` with the id baked in, NOT a bare tuple
# plus a separate ``ids=[...]`` comprehension. The bare-tuple form would
# break the documented ``pytest.param(..., marks=pytest.mark.skip(...),
# id=...)`` escape hatch the moment a single case is wrapped — the ids
# comprehension would then fail to unpack ``pytest.param`` into
# ``(t, n, _)``. Building param objects up front means swapping one entry
# in for a skipped case is a one-line change with no parallel-list
# bookkeeping.
_ORM_CHECK_CASES = [
    pytest.param(table_name, constraint_name, orm_sql, id=f"{table_name}.{constraint_name}")
    for table_name, constraint_name, orm_sql in _ORM_CHECKS
]


# ---------------------------------------------------------------------------
# The main parametrized drift test.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "table_name, constraint_name, orm_sql",
    _ORM_CHECK_CASES,
)
def test_orm_check_constraint_matches_latest_migration(
    table_name: str, constraint_name: str, orm_sql: str
) -> None:
    """Each ORM CHECK constraint must match the latest migration's CHECK.

    Failure modes this catches:

    - ORM was updated, no migration shipped → production schema rejects new
      values (the #568 case before the migration landed).
    - Migration was shipped, ORM not updated → ``Base.metadata.create_all``
      tests reject values that production accepts (the #568 case after the
      migration landed but before the ORM follow-up).
    - Subtle whitespace / quoting drift between the two strings (caught
      after normalization, so this only fires on real semantic drift).
    """
    migration_sql = _MIGRATION_CHECKS.get((table_name, constraint_name))
    assert migration_sql is not None, (
        f"ORM defines CHECK '{constraint_name}' on '{table_name}' but no "
        f"alembic migration creates it. Add a migration with "
        f"op.create_check_constraint(...) or "
        f'op.execute("ALTER TABLE {table_name} ADD CONSTRAINT '
        f'{constraint_name} CHECK (...)").'
    )
    assert orm_sql == migration_sql, (
        f"Drift detected on {table_name}.{constraint_name}:\n"
        f"  ORM      : {orm_sql!r}\n"
        f"  Migration: {migration_sql!r}\n"
        f"Update either backend/src/models/<file>.py __table_args__ or add "
        f"an alembic migration that re-defines the CHECK to match."
    )


# ---------------------------------------------------------------------------
# Named regression: PR #568 / extra_sleep_contexts.
# Lives separately from the parametrized loop so the bug class has a
# permanent, grep-able test name even if the parametrize ids change.
# ---------------------------------------------------------------------------
def test_regression_extra_sleep_contexts_in_addon_check() -> None:
    """PR #568 review loop 1 caught ``extra_sleep_contexts`` missing from
    the ORM CHECK. This test guarantees it stays present on both sides."""
    target = next(
        (
            (table, name, sql)
            for table, name, sql in _ORM_CHECKS
            if table == "workspace_addons" and name == "check_addon_type"
        ),
        None,
    )
    assert target is not None, (
        "WorkspaceAddon.check_addon_type is missing from Base.metadata — "
        "the constraint may have been renamed or dropped."
    )
    _, _, orm_sql = target
    assert "extra_sleep_contexts" in orm_sql, (
        "extra_sleep_contexts (added in PR #568) is missing from the ORM "
        "WorkspaceAddon.__table_args__ CHECK clause."
    )
    migration_sql = _MIGRATION_CHECKS.get(("workspace_addons", "check_addon_type"))
    assert migration_sql is not None, (
        "No alembic migration defines workspace_addons.check_addon_type."
    )
    assert "extra_sleep_contexts" in migration_sql, (
        "extra_sleep_contexts is missing from the latest alembic migration "
        "that defines workspace_addons.check_addon_type."
    )


# ---------------------------------------------------------------------------
# Meta-tests: detector helpers must work on representative inputs.
# ---------------------------------------------------------------------------
def test_normalize_collapses_whitespace_and_trailing_punctuation() -> None:
    assert _normalize("a IN ('x', 'y')") == "a IN ('x', 'y')"
    assert _normalize("a IN ('x',\n  'y')") == "a IN ('x', 'y')"
    assert _normalize("  a IN ('x')  ;") == "a IN ('x')"
    assert _normalize("a   IN\n('x')") == "a IN ('x')"


def test_find_check_clauses_handles_multiline_separate_alter_table() -> None:
    sql = (
        "ALTER TABLE workspace_addons ADD CONSTRAINT check_addon_type\n"
        "        CHECK (addon_type IN ('a', 'b', 'c'))"
    )
    clauses = _find_check_clauses_in_sql(sql)
    assert clauses == [("workspace_addons", "check_addon_type", "addon_type IN ('a', 'b', 'c')")]


def test_find_check_clauses_handles_compound_drop_add() -> None:
    """Compound ``ALTER TABLE t DROP CONSTRAINT …, ADD CONSTRAINT … CHECK
    (…)`` is the zero-downtime DDL pattern in d05_523/d07_495. The ADD
    must pair with the same single ``ALTER TABLE`` even though a DROP sits
    between them."""
    sql = (
        "ALTER TABLE sleep_report_llm_usage "
        "DROP CONSTRAINT IF EXISTS valid_phase, "
        "ADD CONSTRAINT valid_phase CHECK (phase IN ('a', 'b')) NOT VALID"
    )
    clauses = _find_check_clauses_in_sql(sql)
    assert clauses == [("sleep_report_llm_usage", "valid_phase", "phase IN ('a', 'b')")]


def test_find_check_clauses_handles_nested_parens() -> None:
    """A CHECK whose body itself contains parens (e.g. ``IN (…)``) must be
    extracted with paren-balanced matching, not naive non-greedy regex."""
    sql = "ALTER TABLE t ADD CONSTRAINT c CHECK ((a = 1 AND b IN ('x', 'y')) OR c IS NULL)"
    clauses = _find_check_clauses_in_sql(sql)
    assert clauses == [("t", "c", "(a = 1 AND b IN ('x', 'y')) OR c IS NULL")]


def test_normalize_sorts_in_list_literals() -> None:
    """Two CHECKs with the same set of IN-list literals but different
    textual order must normalize equal — order is not semantically
    meaningful for SQL ``IN``."""
    a = _normalize("status IN ('cancelled', 'pending', 'in_progress')")
    b = _normalize("status IN ('pending', 'in_progress', 'cancelled')")
    assert a == b
    # Different sets must NOT normalize equal.
    c = _normalize("status IN ('pending', 'in_progress')")
    assert a != c


def test_normalize_does_not_mangle_compound_in_clauses() -> None:
    """A CHECK with two IN clauses (``a IN (…) AND b IN (…)``) must NOT
    match the IN-list rewrite path — earlier the non-greedy ``.+?`` would
    consume across the inner ``)`` and produce a corrupted literal split.
    Compound clauses fall through to whitespace-only normalization."""
    sql = "a IN ('x', 'y') AND b IN ('z')"
    out = _normalize(sql)
    assert out == sql
    # Sanity: the simple form still gets sorted.
    assert _normalize("a IN ('y', 'x')") == "a IN ('x', 'y')"


def test_extractor_handles_kwargs_create_check_constraint() -> None:
    """``op.create_check_constraint`` accepts its three string arguments
    either positionally or by keyword. The detector must capture both
    forms or it silently drops coverage on kwarg-style migrations."""
    source = (
        "from alembic import op\n"
        "def upgrade():\n"
        "    op.create_check_constraint(\n"
        '        constraint_name="valid_kw",\n'
        '        table_name="t",\n'
        "        condition=\"x IN ('a', 'b')\",\n"
        "    )\n"
    )
    extractor = _MigrationCheckExtractor(source, Path("synthetic.py"))
    assert extractor.checks == [("t", "valid_kw", "x IN ('a', 'b')")]


def test_string_from_ast_resolves_fstring_with_constant() -> None:
    """An f-string with a ``{NAME}`` reference to a known module-level
    string constant resolves; with an unknown name it returns ``None``."""
    tree = ast.parse('FOO = "hello"\nBAR = f"prefix {FOO} suffix"\n')
    constants = {"FOO": "hello"}
    fstring_node = tree.body[1].value  # type: ignore[attr-defined]
    assert _string_from_ast(fstring_node, constants) == "prefix hello suffix"
    # Unknown name → None
    assert _string_from_ast(fstring_node, {}) is None


def test_extractor_walks_calls_in_source_order_across_control_flow() -> None:
    """Within a single migration, ``upgrade()`` must be walked in source
    order (DFS preorder), NOT ``ast.walk`` BFS-by-depth. If a CHECK is
    redefined twice across a control-flow boundary (e.g. inside an ``if``),
    the LAST source-order definition must win — BFS would report the
    later-source call first because the nested call sits one level deeper.
    Copilot loop 4 finding."""
    source = (
        "from alembic import op\n"
        "def upgrade():\n"
        "    if True:\n"
        '        op.create_check_constraint("c", "t", "x IN (\'a\', \'b\')")\n'
        "    op.create_check_constraint(\"c\", \"t\", \"x IN ('a', 'b', 'c')\")\n"
    )
    extractor = _MigrationCheckExtractor(source, Path("synthetic.py"))
    # Source order: nested-call first (inside the if), then top-level. The
    # last entry — the top-level call — is the post-if-branch definition
    # and must win in _build_latest_migration_check_map.
    assert extractor.checks == [
        ("t", "c", "x IN ('a', 'b')"),
        ("t", "c", "x IN ('a', 'b', 'c')"),
    ]


def test_migration_revision_metadata_extracts_revision_chain() -> None:
    """``_migration_revision_metadata`` must extract ``revision`` and
    ``down_revision`` strings from real migration files. Pinning a known
    case — ``a120_add_neural_gating_config.py`` revises ``a90_*`` per its
    ``down_revision`` — guards the AST extractor from regressing on the
    revision chain (Copilot loop 1)."""
    a120 = MIGRATIONS_DIR / "a120_add_neural_gating_config.py"
    assert a120.exists(), "a120 migration is missing — sentinel needs updating"
    rev, downs = _migration_revision_metadata(a120)
    assert rev == "a120_neural_gating"
    assert downs == ["a90_ollama_reranker"]


def test_migrations_in_dependency_order_respects_alembic_chain() -> None:
    """Filename sort and dependency order disagree in this repo:
    ``a120_*`` filename-sorts BEFORE ``a51_*`` (lexicographic ``a1`` < ``a5``)
    but its ``down_revision`` is ``a90_*``. The dependency walk must place
    ``a120_*`` AFTER ``a90_*`` in the returned path list (Copilot loop 1)."""
    paths = _migrations_in_dependency_order()
    names = [p.name for p in paths]
    a90_idx = next(
        (i for i, n in enumerate(names) if n == "a90_add_ollama_reranker_provider.py"),
        None,
    )
    a120_idx = next(
        (i for i, n in enumerate(names) if n == "a120_add_neural_gating_config.py"),
        None,
    )
    assert a90_idx is not None and a120_idx is not None, (
        "a90 / a120 sentinel migrations missing — update the test"
    )
    assert a120_idx > a90_idx, (
        f"dependency walk placed a120 before a90 (a90={a90_idx}, a120={a120_idx}) — "
        f"filename sort is leaking through"
    )


def test_detector_discovers_at_least_one_check_per_known_constraint() -> None:
    """Sanity check: the migration sweep must find the canonical CHECKs we
    know exist. If this fails, the AST extractor is silently dropping
    something and the parametrized test would give false confidence."""
    assert ("workspace_addons", "check_addon_type") in _MIGRATION_CHECKS, (
        "Migration sweep did not find workspace_addons.check_addon_type — "
        "AST extractor may be broken."
    )
    assert ("workspaces", "valid_plan_name") in _MIGRATION_CHECKS, (
        "Migration sweep did not find workspaces.valid_plan_name."
    )
    assert ("users", "valid_role") in _MIGRATION_CHECKS, (
        "Migration sweep did not find users.valid_role."
    )
