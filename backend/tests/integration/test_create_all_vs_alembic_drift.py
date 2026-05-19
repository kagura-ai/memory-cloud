"""DDL drift detector: ``Base.metadata.create_all()`` vs ``alembic upgrade head``.

Issue #613 (sister of #587 / PR #590).

This test catches the class of bug fixed in PR #610: a ``server_default``
added to the alembic migration (or to the ORM ``mapped_column(...)``) but
missing on the other side. ``Base.metadata.create_all()`` is the path
exercised by fresh dev DBs and a number of integration test fixtures;
alembic-driven production is the canonical reference. When the two paths
diverge, the divergence is silent until a raw ``INSERT`` (test fixture,
bulk loader, future migration) trips a ``NOT NULL`` violation because the
documented default was never wired through.

**What this detector covers**

For every user-defined table in the ``public`` schema (excluding the
``alembic_version`` bookkeeping table), the detector compares:

1. **Columns** — name, data type, nullability, normalized ``column_default``.
2. **Primary keys** — column membership AND column order. Although the
   uniqueness property itself is set-based, column order matters for the
   B-tree index that backs the PK (prefix-scan plans differ between
   ``PK (a, b)`` and ``PK (b, a)``), so composite PK order is treated
   as drift-significant.
3. **Unique constraints** — keyed by constraint name, columns in
   ordinal-position order (same rationale as section 2 for composite
   UKs).
4. **Foreign keys** — keyed by constraint name, comparing local columns,
   referenced table, and referenced columns.
5. **Indexes** — keyed by index name, comparing the column list +
   uniqueness + partial-index predicate. **PK auto-indexes are filtered
   out** (already covered by section 2). UK auto-indexes ARE kept,
   because their column ordering and partial predicates carry drift
   signal that the constraint-level (section 3) comparison cannot
   surface — see ``_introspect_indexes`` docstring for details. As a
   consequence, UK constraints can appear in BOTH the index map and
   the constraints map; that pairing is by design.

**What this detector explicitly does NOT cover**

- **CHECK constraints** — handled by the sister detector at
  ``backend/tests/test_schema_drift.py`` (#587 / PR #590) via AST diff,
  which catches divergences that pure information_schema introspection
  cannot (e.g. function-call SQL or migration-only sentinels).
- **Sequence ownership** (``ALTER SEQUENCE … OWNED BY``) — emitted by
  alembic for ``BIGSERIAL`` surrogates but not surfaced through
  ``information_schema``. Both snapshots see the same ``nextval(...)``
  column default, so functional equivalence is preserved.
- **ENUM value set (label additions / removals)** —
  ``information_schema.columns.data_type`` returns the literal string
  ``USER-DEFINED`` for every ENUM, but ``udt_name`` carries the actual
  ENUM type name, so type-rename drift IS now caught. **Value set
  drift** (e.g. adding ``'pending'`` to an existing ``status_enum``)
  requires ``pg_enum`` introspection and remains out of scope; it
  belongs to its own audit.
- **Function-call defaults whose Postgres normalization is non-stable**
  (e.g. ``now()`` vs ``CURRENT_TIMESTAMP``). When both paths use the
  same SQL, Postgres normalizes both to the same stored representation,
  so the comparison is symmetric. When they use different SQL with the
  same semantic meaning, that IS a drift the detector should flag —
  intentional cases land in ``_KNOWN_DRIFT`` with a reason.

**Escape hatch (false positives)**

Add the offending ``stable_id`` to ``_KNOWN_DRIFT`` with a comment
citing the issue or PR that authorized the deviation. The format is
``"<table>.<name>.<kind>.<side>"`` — see ``_stable_id`` for the
construction rules. Adding entries without a reason is a code smell;
review will push back.

**Performance**

The test does two full schema reset-and-rebuild cycles (``DROP SCHEMA
public CASCADE`` → ``create_all`` → ``DROP SCHEMA public CASCADE`` →
``alembic upgrade head``). On local Docker this is roughly 3-8 seconds
end-to-end. The test ends with the DB at alembic head, which is the
canonical post-test state every other integration test relies on, so
no explicit cleanup is needed.

**Session-fixture caveat**

The test session-scoped ``async_engine`` fixture in
``backend/tests/conftest.py`` calls ``create_all`` once at session start.
``_reset_alembic_state()`` wipes that. Per the precedent in
``test_alembic_migrations.py`` and ``test_resources_foundation_migration.py``,
the discipline is to leave the DB at ``alembic upgrade head`` on exit —
which the second phase of this test does naturally. Do not run this
test in parallel with other DDL-touching integration tests.
"""

from __future__ import annotations

import importlib
import re
from typing import TypedDict

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from db.base import Base

from .test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Import all model modules so ``Base.metadata`` is fully populated for
# ``create_all``. Mirrors the pattern at the top of
# ``backend/tests/test_schema_drift.py``. The model modules are imported
# for side effect only — pyright / ruff "unused import" suppression is
# achieved via the loop (no attribute is dereferenced by name).
_MODEL_MODULES: tuple[str, ...] = (
    "models.analysis",
    "models.auth",
    "models.bm25_drift",
    "models.config",
    "models.erasure",
    "models.file_objects",
    "models.hub_tag",
    "models.llm_call_log",
    "models.llm_pricing",
    "models.memory",
    "models.neural",
    "models.resource",
    "models.signup_gate",
    "models.sleep",
)
for _model_module_name in _MODEL_MODULES:
    importlib.import_module(_model_module_name)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Strip chained ``::type_cast`` suffixes from information_schema
# ``column_default`` values so e.g. ``'skip'::character varying`` and
# ``'skip'`` compare equal. The ``+`` covers chained casts like
# ``'value'::text::character varying``.
_TYPE_CAST_RE = re.compile(r"(::[a-z][a-z0-9_ ]*)+\s*$", re.IGNORECASE)

# Tables that are structurally created by alembic but never by
# ``Base.metadata.create_all``. Compared sides differ by construction;
# filtering here is correct (not an allowlist concern).
_EXCLUDED_TABLES: frozenset[str] = frozenset({"alembic_version"})

# Known drift discovered on the first live run of this detector
# (#613 baseline, 34 entries across 5 categories). Each entry is grouped
# by category with a comment citing the root cause and the follow-up
# issue that will eliminate it. As each follow-up PR lands and the
# underlying drift is resolved, the corresponding entry MUST be deleted
# from this set — leaving the entry in place after the fix would mask a
# regression. Format: ``<table>.<name>.<kind>.<side>``.
_KNOWN_DRIFT: frozenset[str] = frozenset(
    {
        # Cat A: server_default drift — RESOLVED (PR #728 #616 + PR-B #613 Cat A residual).
        # ─── Cat B: FK naming-convention drift (14 entries / 7 pairs) ───
        # SQLAlchemy auto-names FK constraints ``<table>_<col>_fkey``
        # while the alembic migrations assigned explicit ``fk_<table>_*``
        # names. Both sides describe the SAME foreign key relationship,
        # but the names differ, so the detector reports each as a
        # create_all_only / alembic_only pair. Root fix: configure
        # ``Base.metadata.naming_convention`` to match the alembic
        # convention so SQLAlchemy and alembic emit identical names.
        # Follow-up: FK naming-convention unification Cat B.
        "api_keys.api_keys_bound_context_id_fkey.constraint.create_all_only",
        "indexer_state.indexer_state_resource_pk_fkey.constraint.create_all_only",
        "oauth_device_codes.oauth_device_codes_client_id_fkey.constraint.create_all_only",
        "resource_events.resource_events_resource_pk_fkey.constraint.create_all_only",
        "resource_schemas.resource_schemas_resource_pk_fkey.constraint.create_all_only",
        "resource_tokens.resource_tokens_resource_pk_fkey.constraint.create_all_only",
        "resource_tokens.resource_tokens_workspace_id_fkey.constraint.create_all_only",
        "api_keys.fk_api_keys_bound_context_id.constraint.alembic_only",
        "indexer_state.fk_indexer_state_resource_pk.constraint.alembic_only",
        "oauth_device_codes.fk_oauth_device_codes_client_id.constraint.alembic_only",
        "resource_events.fk_resource_events_resource_pk.constraint.alembic_only",
        "resource_schemas.fk_resource_schemas_resource_pk.constraint.alembic_only",
        "resource_tokens.fk_resource_tokens_resource_pk.constraint.alembic_only",
        "resource_tokens.fk_resource_tokens_workspace.constraint.alembic_only",
        # ─── Cat C: UK naming drift (2 entries) ─────────────────────────
        # Same root cause as Cat B but for UNIQUE constraints on
        # ``oauth_device_codes``. Alembic emits explicit named UK
        # constraints; the ORM only declares ``unique=True`` on the
        # column, which Postgres records as an implicit unique index
        # with no named UK constraint. Resolution: declare an explicit
        # ``UniqueConstraint(...)`` in the ORM model OR drop the named
        # UK from alembic when the naming-convention work in Cat B
        # makes the constraint names match.
        # Follow-up: UK naming alignment Cat C (likely folds into Cat B).
        "oauth_device_codes.oauth_device_codes_device_code_key.constraint.alembic_only",
        "oauth_device_codes.oauth_device_codes_user_code_key.constraint.alembic_only",
        # ─── Cat D: migration-only indexes (7 entries) ──────────────────
        # Indexes that alembic creates but ``Base.metadata.create_all``
        # does not. Causes (per index):
        #   * idx_memories_external_blob_ref: partial WHERE clause not
        #     declared in the ORM Index() call (or no Index() at all)
        #   * idx_memories_source_uri: same partial-WHERE gap
        #   * idx_memories_tags_gin: GIN access method not declared in
        #     the ORM via ``postgresql_using='gin'``
        #   * idx_memory_analysis_assignments_analysis_cluster: composite
        #     index missing from ORM
        #   * oauth_device_codes_*_key (×2): UK auto-indexes paired with
        #     the Cat C UK names; resolved with Cat C
        #   * ux_contexts_resource_id_active: partial UNIQUE with
        #     compound WHERE not declared in the ORM
        # Resolution: declare matching ``Index(...)`` / ``UniqueConstraint``
        # entries in the ORM models, or accept these as migration-only
        # and document them as such in the model docstring.
        # Follow-up: ORM index parity audit Cat D.
        "memories.idx_memories_external_blob_ref.index.alembic_only",
        "memories.idx_memories_source_uri.index.alembic_only",
        "memories.idx_memories_tags_gin.index.alembic_only",
        "memory_analysis_assignments.idx_memory_analysis_assignments_analysis_cluster.index.alembic_only",
        "oauth_device_codes.oauth_device_codes_device_code_key.index.alembic_only",
        "oauth_device_codes.oauth_device_codes_user_code_key.index.alembic_only",
        "contexts.ux_contexts_resource_id_active.index.alembic_only",
        # ─── Cat E: uniqueness / expression drift (3 entries — fix ORM side) ───
        # The ORM and alembic disagree on the uniqueness flag or column
        # expression of indexes that share a name. Production (alembic)
        # is the canonical truth; the fix is to align the ORM model.
        #   * ix_oauth_device_codes_device_code (is_unique=True/False)
        #   * ix_oauth_device_codes_user_code   (is_unique=True/False)
        #     → ORM declares ``unique=True, index=True``; alembic emits
        #       ``op.create_index(unique=False)``. **Not a correctness
        #       gap** — migration ``d08_536_device_code_grant`` already
        #       declares ``device_code`` / ``user_code`` columns with
        #       ``unique=True``, so uniqueness is enforced via the
        #       implicit UNIQUE constraint + backing unique index. The
        #       drift is index redundancy: the ORM's ``unique=True,
        #       index=True`` produces two distinct unique indexes per
        #       column, while alembic emits one unique-backing + one
        #       non-unique secondary. Fix: drop ``unique=True`` from
        #       the ORM ``mapped_column`` (keep ``index=True`` only)
        #       so it matches alembic's "lookup index over an already-
        #       unique column" intent.
        #   * uq_file_objects_workspace_sha256_active (column expression)
        #     → ORM declares UNIQUE ``(workspace_id, sha256)``
        #       (case-sensitive). Alembic produces UNIQUE
        #       ``(workspace_id, lower(sha256::text))`` (case-insensitive,
        #       per migration ``e07_556_sha256_lowercase_index`` — #556
        #       follow-up). NOT a data-integrity bug: production correctly
        #       enforces case-insensitive sha256 dedup. The drift is that
        #       the ORM ``UniqueConstraint`` declaration didn't migrate
        #       alongside the index change. Fix: declare
        #       ``UniqueConstraint('workspace_id', func.lower(sha256), ...)``
        #       on the model.
        # Follow-up: uniqueness / expression drift fix Cat E (alignment,
        # not data-integrity correction).
        "oauth_device_codes.ix_oauth_device_codes_device_code.index.value_mismatch",
        "oauth_device_codes.ix_oauth_device_codes_user_code.index.value_mismatch",
        "file_objects.uq_file_objects_workspace_sha256_active.index.value_mismatch",
    }
)


# ──────────────────────────────────────────────────────────────────────────────
# TypedDicts
# ──────────────────────────────────────────────────────────────────────────────


class ColumnRow(TypedDict):
    """One column's introspected shape from ``information_schema.columns``.

    Beyond ``data_type``, the snapshot carries ``character_maximum_length``,
    ``numeric_precision`` / ``numeric_scale``, and ``datetime_precision``
    so that drift in fully-qualified types like ``NUMERIC(14, 10)`` or
    ``TIMESTAMP(6)`` is caught — ``data_type`` alone would compare equal.

    ``udt_name`` / ``udt_schema`` carry the **underlying type name** for
    ``data_type`` values that lose information (notably ``ARRAY`` —
    ``udt_name`` becomes ``_int4`` / ``_text`` etc. with a leading
    underscore; and ``USER-DEFINED`` — ``udt_name`` becomes the ENUM /
    domain type name). Comparing these catches ARRAY element-type drift
    and ENUM type-name drift that ``data_type`` alone misses.
    """

    table: str
    column: str
    data_type: str
    is_nullable: str  # "YES" | "NO"
    column_default: str | None
    character_maximum_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    datetime_precision: int | None
    udt_name: str
    udt_schema: str


class ConstraintRow(TypedDict):
    """One PK / UK / FK constraint, normalized for comparison.

    Column order is **preserved** for PK and UK constraints (sorted by
    ``information_schema.key_column_usage.ordinal_position``) so that
    composite PKs and UKs with different column order on the two sides
    surface as drift — column order matters for B-tree prefix scans
    even though the uniqueness property itself is set-based.

    For FOREIGN KEY constraints, ``information_schema.constraint_column_usage``
    returns a Cartesian product per constraint, so positional order is
    not reliable; ``_introspect_fk`` accepts this as a known limitation
    (no multi-column FKs in the current schema; see ``_introspect_fk``
    docstring for the fix plan when needed).

    ``update_rule`` / ``delete_rule`` carry the FK referential actions
    (``NO ACTION`` / ``RESTRICT`` / ``CASCADE`` / ``SET NULL`` /
    ``SET DEFAULT``) from ``information_schema.referential_constraints``;
    these are ``None`` for PK / UK rows.
    """

    table: str
    name: str
    kind: str  # "PRIMARY KEY" | "UNIQUE" | "FOREIGN KEY"
    columns: tuple[str, ...]
    foreign_table: str | None
    foreign_columns: tuple[str, ...] | None
    update_rule: str | None  # FK only — ON UPDATE action
    delete_rule: str | None  # FK only — ON DELETE action


class IndexRow(TypedDict):
    """One index excluding PK auto-indexes.

    ``access_method`` is the index access method (``btree`` / ``gin`` /
    ``gist`` / ``hash`` / ``brin`` / ``spgist``) from ``pg_am.amname``.
    Comparing it catches drift where an ORM-declared ``Index`` defaults
    to ``btree`` while alembic uses ``postgresql_using='gin'`` for the
    same name + columns.
    """

    table: str
    name: str
    columns: tuple[str, ...]
    is_unique: bool
    predicate: str | None  # WHERE clause for partial indexes
    access_method: str  # btree | gin | gist | hash | brin | spgist


class SchemaSnapshot(TypedDict):
    """All structural facts about one database state."""

    columns: dict[tuple[str, str], ColumnRow]  # (table, column) -> row
    constraints: dict[str, ConstraintRow]  # name -> row
    indexes: dict[str, IndexRow]  # name -> row


class DriftRecord(TypedDict):
    """One observed drift between two snapshots."""

    kind: str  # "column" | "constraint" | "index"
    table: str
    name: str
    side: str  # "create_all_only" | "alembic_only" | "value_mismatch"
    detail: str  # human-readable description


# ──────────────────────────────────────────────────────────────────────────────
# Normalization helpers
# ──────────────────────────────────────────────────────────────────────────────


def _strip_type_cast(default: str | None) -> str | None:
    """Remove trailing ``::type`` cast(s) from a column_default value.

    >>> _strip_type_cast("'skip'::character varying")
    "'skip'"
    >>> _strip_type_cast("nextval('users_id_seq'::regclass)")
    "nextval('users_id_seq'::regclass)"  # cast inside parens preserved
    >>> _strip_type_cast(None)
    None

    Note on the empty-string return: when ``default`` is non-None but the
    regex strips it down to a zero-length string, ``None`` is returned.
    This collapses two distinct postgres states — ``column_default IS
    NULL`` and ``column_default = ''`` — into the same Python ``None``.
    The empty-string-default case has no known trigger in this schema
    (SQL string literals always carry surrounding quotes like ``''``,
    which the regex does not strip). If a future model declares
    ``server_default=text("")`` and round-trips as a zero-length
    ``column_default`` from postgres, this collapse becomes a real
    false-positive source — at which point return ``""`` explicitly
    here and document the trigger.
    """
    if default is None:
        return None
    return _TYPE_CAST_RE.sub("", default).strip() or None


# ──────────────────────────────────────────────────────────────────────────────
# Per-category introspection
# ──────────────────────────────────────────────────────────────────────────────

# All queries below use ``sqlalchemy.text(...)`` with bound parameters where
# user-supplied values exist. The literal SQL bodies contain only static
# tokens and information_schema / pg_catalog identifiers — no string
# interpolation of external input.

_COLUMNS_SQL = """
    SELECT
        table_name,
        column_name,
        data_type,
        is_nullable,
        column_default,
        character_maximum_length,
        numeric_precision,
        numeric_scale,
        datetime_precision,
        udt_name,
        udt_schema
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position
"""

_PK_UK_SQL = """
    SELECT
        tc.table_name,
        tc.constraint_name,
        tc.constraint_type,
        kcu.column_name,
        kcu.ordinal_position
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
        AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public'
        AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
    ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position
"""

_FK_SQL = """
    SELECT
        tc.table_name AS local_table,
        tc.constraint_name,
        kcu.column_name AS local_column,
        kcu.ordinal_position,
        ccu.table_name AS foreign_table,
        ccu.column_name AS foreign_column,
        rc.update_rule,
        rc.delete_rule
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
        AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
        ON ccu.constraint_name = tc.constraint_name
        AND ccu.table_schema = tc.table_schema
    JOIN information_schema.referential_constraints rc
        ON rc.constraint_name = tc.constraint_name
        AND rc.constraint_schema = tc.table_schema
    WHERE tc.table_schema = 'public'
        AND tc.constraint_type = 'FOREIGN KEY'
    ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position
"""

# pg_index gives us indkey (column ordinal list), indisunique, and
# pg_get_expr(indpred, ...) for the partial WHERE clause. pg_indexes
# alone would suffice for indexname / indexdef, but we want columns in
# array form rather than parsing them out of indexdef.
_INDEXES_SQL = """
    SELECT
        c.relname AS table_name,
        i.relname AS index_name,
        ix.indisunique AS is_unique,
        ix.indisprimary AS is_primary,
        am.amname AS access_method,
        ARRAY(
            SELECT pg_get_indexdef(ix.indexrelid, k.ord::int, true)
            FROM generate_series(1, ix.indnatts) WITH ORDINALITY AS k(_, ord)
            ORDER BY k.ord
        ) AS columns,
        pg_get_expr(ix.indpred, ix.indrelid) AS predicate
    FROM pg_index ix
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_class c ON c.oid = ix.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = i.relam
    WHERE n.nspname = 'public'
        AND NOT ix.indisprimary
    ORDER BY c.relname, i.relname
"""


def _introspect_columns(conn: Connection) -> dict[tuple[str, str], ColumnRow]:
    """Return ``{(table, column): ColumnRow}`` from information_schema."""
    result: dict[tuple[str, str], ColumnRow] = {}
    rows = conn.execute(text(_COLUMNS_SQL))
    for row in rows:
        table = row.table_name
        if table in _EXCLUDED_TABLES:
            continue
        col = row.column_name
        result[(table, col)] = ColumnRow(
            table=table,
            column=col,
            data_type=row.data_type,
            is_nullable=row.is_nullable,
            column_default=_strip_type_cast(row.column_default),
            character_maximum_length=row.character_maximum_length,
            numeric_precision=row.numeric_precision,
            numeric_scale=row.numeric_scale,
            datetime_precision=row.datetime_precision,
            udt_name=row.udt_name,
            udt_schema=row.udt_schema,
        )
    return result


def _introspect_pk_uk(conn: Connection) -> dict[str, ConstraintRow]:
    """Return ``{constraint_name: ConstraintRow}`` for PK + UK constraints."""
    # Aggregate columns by constraint_name, preserving table + kind.
    raw: dict[str, dict] = {}
    rows = conn.execute(text(_PK_UK_SQL))
    for row in rows:
        if row.table_name in _EXCLUDED_TABLES:
            continue
        entry = raw.setdefault(
            row.constraint_name,
            {
                "table": row.table_name,
                "kind": row.constraint_type,
                "columns": [],
            },
        )
        entry["columns"].append(row.column_name)

    result: dict[str, ConstraintRow] = {}
    for name, entry in raw.items():
        # Preserve ordinal_position order from the SQL ORDER BY — column
        # order matters for composite PK / UK B-tree prefix scans, so
        # do NOT sort here.
        result[name] = ConstraintRow(
            table=entry["table"],
            name=name,
            kind=entry["kind"],
            columns=tuple(entry["columns"]),
            foreign_table=None,
            foreign_columns=None,
            update_rule=None,
            delete_rule=None,
        )
    return result


def _introspect_fk(conn: Connection) -> dict[str, ConstraintRow]:
    """Return ``{constraint_name: ConstraintRow}`` for FOREIGN KEY constraints.

    **Known limitation (multi-column FK column ordering)**: the underlying
    ``information_schema.constraint_column_usage`` view returns a Cartesian
    product of local × foreign columns per constraint. The dedup-then-sort
    aggregation below loses positional information for multi-column FKs:
    ``FK(local=(a,b), foreign=(x,y))`` and the hypothetical mismatched
    ``FK(local=(a,b), foreign=(y,x))`` would produce identical normalized
    snapshots. Acceptable in this repo today (no multi-column FKs in the
    current schema as of #613). When a multi-column FK is added, replace
    this implementation with one that joins ``pg_constraint.conkey`` /
    ``confkey`` arrays to preserve column position deterministically.
    """
    raw: dict[str, dict] = {}
    rows = conn.execute(text(_FK_SQL))
    for row in rows:
        if row.local_table in _EXCLUDED_TABLES:
            continue
        entry = raw.setdefault(
            row.constraint_name,
            {
                "table": row.local_table,
                "foreign_table": row.foreign_table,
                "local_columns": [],
                "foreign_columns": [],
                "update_rule": row.update_rule,
                "delete_rule": row.delete_rule,
            },
        )
        # Avoid duplicates from constraint_column_usage's cross-join shape.
        if row.local_column not in entry["local_columns"]:
            entry["local_columns"].append(row.local_column)
        if row.foreign_column not in entry["foreign_columns"]:
            entry["foreign_columns"].append(row.foreign_column)

    result: dict[str, ConstraintRow] = {}
    for name, entry in raw.items():
        result[name] = ConstraintRow(
            table=entry["table"],
            name=name,
            kind="FOREIGN KEY",
            columns=tuple(sorted(entry["local_columns"])),
            foreign_table=entry["foreign_table"],
            foreign_columns=tuple(sorted(entry["foreign_columns"])),
            update_rule=entry["update_rule"],
            delete_rule=entry["delete_rule"],
        )
    return result


def _introspect_indexes(conn: Connection) -> dict[str, IndexRow]:
    """Return ``{index_name: IndexRow}`` for non-PK indexes.

    PK auto-indexes are filtered (``NOT ix.indisprimary`` in the SQL).
    UK auto-indexes ARE included here because their drift in column
    ordering / partial predicate is a real signal — UK names typically
    match the constraint name, so their presence here is harmless
    duplication with the UK section, and their absence here would mask
    real drift in unique-index predicates.

    **Expression-aware columns**: ``columns`` is built by calling
    ``pg_get_indexdef(indexrelid, position, true)`` for each position
    1..``indnatts``. This returns the column name for regular columns
    AND the rendered expression text (e.g. ``lower(sha256)``) for
    functional indexes. The earlier ``unnest(indkey) JOIN pg_attribute``
    form silently dropped expression columns because Postgres represents
    them with ``indkey.attnum = 0`` and stores the expression separately
    in ``pg_index.indexprs``.
    """
    result: dict[str, IndexRow] = {}
    rows = conn.execute(text(_INDEXES_SQL))
    for row in rows:
        if row.table_name in _EXCLUDED_TABLES:
            continue
        result[row.index_name] = IndexRow(
            table=row.table_name,
            name=row.index_name,
            columns=tuple(row.columns),  # ordinal order preserved
            is_unique=row.is_unique,
            predicate=row.predicate,
            access_method=row.access_method,
        )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot orchestration
# ──────────────────────────────────────────────────────────────────────────────


def _capture_schema(conn: Connection) -> SchemaSnapshot:
    """Capture the full schema state of the public schema.

    Combines per-category introspection into one snapshot:

    - ``columns`` — keyed by ``(table, column)``
    - ``constraints`` — **single merged map** of PK + UK + FK, keyed by
      constraint name. Merging is safe because postgres enforces
      constraint-name uniqueness within a schema, so no PK / UK / FK
      can share a name. Comparison is per-attribute (see
      ``ConstraintRow``), so the merge does not cause kind drift to
      surface as anything other than the intended ``kind`` mismatch.
    - ``indexes`` — keyed by index name (also schema-unique in postgres)
    """
    pk_uk = _introspect_pk_uk(conn)
    fk = _introspect_fk(conn)
    return SchemaSnapshot(
        columns=_introspect_columns(conn),
        constraints={**pk_uk, **fk},
        indexes=_introspect_indexes(conn),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Diff
# ──────────────────────────────────────────────────────────────────────────────

# The three ``_diff_*`` functions below share an ~80% structural clone:
# "items in A only" / "items in B only" / "items in both with value
# mismatch". Consolidating them into a generic ``_diff_map(kind, a, b,
# *, attrs, key_to_table_name)`` would save ~60 LOC but at the cost of
# two callback parameters whose readability cost is comparable to the
# duplication it removes. Deferring to a follow-up refactor when a
# fourth diff category is added (e.g. ENUM body, sequence ownership)
# and the abstraction has more than three call sites earning it.


def _stable_id(record: DriftRecord) -> str:
    """Produce a stable, grep-able ID for an allowlist entry.

    Format: ``<table>.<name>.<kind>.<side>``. Used for both human-readable
    failure messages and ``_KNOWN_DRIFT`` membership tests.

    >>> _stable_id(DriftRecord(
    ...     kind="column", table="users", name="email",
    ...     side="value_mismatch", detail="...",
    ... ))
    'users.email.column.value_mismatch'
    """
    return f"{record['table']}.{record['name']}.{record['kind']}.{record['side']}"


def _diff_columns(
    a: dict[tuple[str, str], ColumnRow],
    b: dict[tuple[str, str], ColumnRow],
) -> list[DriftRecord]:
    """Diff two column maps; report missing rows + per-attribute mismatches."""
    drifts: list[DriftRecord] = []
    a_keys = set(a)
    b_keys = set(b)

    for key in sorted(a_keys - b_keys):
        table, col = key
        drifts.append(
            DriftRecord(
                kind="column",
                table=table,
                name=col,
                side="create_all_only",
                detail=f"column exists in create_all but not in alembic head: {a[key]!r}",
            )
        )
    for key in sorted(b_keys - a_keys):
        table, col = key
        drifts.append(
            DriftRecord(
                kind="column",
                table=table,
                name=col,
                side="alembic_only",
                detail=f"column exists in alembic head but not in create_all: {b[key]!r}",
            )
        )
    for key in sorted(a_keys & b_keys):
        table, col = key
        row_a = a[key]
        row_b = b[key]
        # Compare each attribute we care about; report value_mismatch
        # only if at least one attribute differs.
        diffs: list[str] = []
        for attr in (
            "data_type",
            "is_nullable",
            "column_default",
            "character_maximum_length",
            "numeric_precision",
            "numeric_scale",
            "datetime_precision",
            "udt_name",
            "udt_schema",
        ):
            if row_a[attr] != row_b[attr]:
                diffs.append(f"{attr}: create_all={row_a[attr]!r}, alembic={row_b[attr]!r}")
        if diffs:
            drifts.append(
                DriftRecord(
                    kind="column",
                    table=table,
                    name=col,
                    side="value_mismatch",
                    detail="; ".join(diffs),
                )
            )
    return drifts


def _diff_constraints(
    a: dict[str, ConstraintRow],
    b: dict[str, ConstraintRow],
) -> list[DriftRecord]:
    """Diff two constraint maps (PK + UK + FK combined)."""
    drifts: list[DriftRecord] = []
    a_keys = set(a)
    b_keys = set(b)

    for name in sorted(a_keys - b_keys):
        row = a[name]
        drifts.append(
            DriftRecord(
                kind="constraint",
                table=row["table"],
                name=name,
                side="create_all_only",
                detail=f"{row['kind']} {name} on {row['table']} exists in create_all only: {row!r}",
            )
        )
    for name in sorted(b_keys - a_keys):
        row = b[name]
        drifts.append(
            DriftRecord(
                kind="constraint",
                table=row["table"],
                name=name,
                side="alembic_only",
                detail=f"{row['kind']} {name} on {row['table']} exists in alembic only: {row!r}",
            )
        )
    for name in sorted(a_keys & b_keys):
        row_a = a[name]
        row_b = b[name]
        diffs: list[str] = []
        for attr in (
            "kind",
            "columns",
            "foreign_table",
            "foreign_columns",
            "update_rule",
            "delete_rule",
        ):
            if row_a[attr] != row_b[attr]:
                diffs.append(f"{attr}: create_all={row_a[attr]!r}, alembic={row_b[attr]!r}")
        if diffs:
            drifts.append(
                DriftRecord(
                    kind="constraint",
                    table=row_a["table"],
                    name=name,
                    side="value_mismatch",
                    detail="; ".join(diffs),
                )
            )
    return drifts


def _diff_indexes(
    a: dict[str, IndexRow],
    b: dict[str, IndexRow],
) -> list[DriftRecord]:
    """Diff two index maps."""
    drifts: list[DriftRecord] = []
    a_keys = set(a)
    b_keys = set(b)

    for name in sorted(a_keys - b_keys):
        row = a[name]
        drifts.append(
            DriftRecord(
                kind="index",
                table=row["table"],
                name=name,
                side="create_all_only",
                detail=f"index {name} on {row['table']} exists in create_all only: {row!r}",
            )
        )
    for name in sorted(b_keys - a_keys):
        row = b[name]
        drifts.append(
            DriftRecord(
                kind="index",
                table=row["table"],
                name=name,
                side="alembic_only",
                detail=f"index {name} on {row['table']} exists in alembic only: {row!r}",
            )
        )
    for name in sorted(a_keys & b_keys):
        row_a = a[name]
        row_b = b[name]
        diffs: list[str] = []
        for attr in ("columns", "is_unique", "predicate", "access_method"):
            if row_a[attr] != row_b[attr]:
                diffs.append(f"{attr}: create_all={row_a[attr]!r}, alembic={row_b[attr]!r}")
        if diffs:
            drifts.append(
                DriftRecord(
                    kind="index",
                    table=row_a["table"],
                    name=name,
                    side="value_mismatch",
                    detail="; ".join(diffs),
                )
            )
    return drifts


def _diff_snapshots(a: SchemaSnapshot, b: SchemaSnapshot) -> list[DriftRecord]:
    """Return the full sorted drift list between two snapshots.

    ``a`` is interpreted as the ``create_all`` side, ``b`` as the
    ``alembic upgrade head`` side. The ``side`` field on each
    ``DriftRecord`` is set accordingly.
    """
    return [
        *_diff_columns(a["columns"], b["columns"]),
        *_diff_constraints(a["constraints"], b["constraints"]),
        *_diff_indexes(a["indexes"], b["indexes"]),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Test entry point
# ──────────────────────────────────────────────────────────────────────────────


class TestCreateAllVsAlembicDrift:
    """Compare ``Base.metadata.create_all()`` vs ``alembic upgrade head``.

    Single test method. All drift cases are reported together in one
    aggregated assertion message; the test does not use ``parametrize``
    because the source data requires a live DB and would have to be
    captured at module-import time (a brittle pattern for integration
    tests).
    """

    def test_no_drift_between_create_all_and_alembic_upgrade_head(self) -> None:
        # Phase 1 — create_all snapshot
        _reset_alembic_state()
        engine = _sync_engine()
        try:
            Base.metadata.create_all(engine)
            with engine.connect() as conn:
                create_all_snap = _capture_schema(conn)
        finally:
            engine.dispose()

        # Phase 2 — alembic head snapshot. The second reset wipes the
        # create_all state and gives alembic a clean baseline. The
        # ``_alembic_at_test_db()`` wrapper is mandatory; without it
        # alembic env.py reads the dev DB URL and migrations run
        # against the wrong target (see _alembic_at_test_db docstring).
        _reset_alembic_state()
        config = _get_alembic_config()
        with _alembic_at_test_db():
            command.upgrade(config, "head")

        engine = _sync_engine()
        try:
            with engine.connect() as conn:
                alembic_snap = _capture_schema(conn)
        finally:
            engine.dispose()

        # Phase 3 — diff + allowlist filter + assert
        drifts = _diff_snapshots(create_all_snap, alembic_snap)
        actionable = [d for d in drifts if _stable_id(d) not in _KNOWN_DRIFT]

        if actionable:
            lines = [f"  [{_stable_id(d)}] {d['detail']}" for d in actionable]
            msg = (
                f"{len(actionable)} schema drift(s) between "
                f"Base.metadata.create_all() and alembic upgrade head:\n"
                + "\n".join(lines)
                + "\n\nIf an entry is intentional, add its stable_id to "
                "_KNOWN_DRIFT with a comment citing the issue or PR."
            )
            pytest.fail(msg)
        # DB is at alembic head from Phase 2 — the post-condition every
        # other integration test expects. No explicit cleanup needed.
