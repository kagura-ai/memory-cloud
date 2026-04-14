"""Introduce resources entity + resource_pk FK + 3 UNIQUE constraints.

Issue #323: Normalize the resource subsystem. Add an authoritative
``resources`` table keyed by UUID with a ``workspace_id`` FK, then
introduce ``resource_pk UUID`` FK columns on the four existing tables
that currently key themselves by ``resource_id VARCHAR`` alone
(``resource_events``, ``resource_schemas``, ``indexer_state``,
``resource_tokens``). Finally add the three UNIQUE constraints the model
docstrings have always promised but the baseline migration never
materialized, and add/backfill ``resource_tokens.workspace_id`` as a
nullable FK shadow column ahead of follow-up NOT NULL enforcement in
#325 (so tenancy is ultimately enforced at the schema layer, once all
writers have been migrated to populate it — see the Phase 1 section
below).

Revision ID: a97_resources_entity
Revises: a96_ctx_resource_id_unique

NOTE: The revision ID is capped at 32 characters because
``alembic_version.version_num`` is ``VARCHAR(32)`` in this database
(asyncpg raises ``StringDataRightTruncationError`` otherwise).

PHASE 1 OF 2 — SHADOW-COLUMN ROLLOUT
------------------------------------
This migration is the schema half of the resources normalization
refactor. It **adds** ``resource_pk`` and ``resource_tokens.workspace_id``
as *nullable* shadow columns and backfills them from existing data, but
does **not** tighten them to NOT NULL. Tightening lives in a follow-up
(#325 in the same epic) so the intervening release can ship application
code updates (#324) that populate the new columns on every write.

If ``resource_pk`` were tightened to NOT NULL here, every existing write
path that only supplies ``resource_id VARCHAR`` would start throwing
``NullViolationError`` on the first request after deploy — the whole
point of the shadow phase is to let old writers continue working while
new writers migrate over.

DUAL-WRITE PROHIBITION (forward-looking)
----------------------------------------
Once application writers are updated to populate ``resource_pk``, they
MUST NOT write ``resource_id`` independently. The ``resource_id``
VARCHAR column, while it still exists, should only be set via a shim
that looks up the slug through ``resources.id = resource_pk``.
Independent writes will silently diverge and break the invariant this
migration is preparing. The legacy ``resource_id`` column will be
dropped once every writer has switched over (tracked under epic #321).

UNIQUE constraint semantics during Phase 1
-------------------------------------------
The three UNIQUE constraints are added as **partial indexes** with
``WHERE resource_pk IS NOT NULL`` (composed with ``op = 'upsert'`` on
``resource_events``). During Phase 1 this protects backfilled rows and
any new writes that correctly populate ``resource_pk``; rows that still
write only ``resource_id`` with ``resource_pk = NULL`` are unprotected
— matching the pre-migration status quo, so no regression. After #325
tightens the columns to NOT NULL, the partial predicate becomes
universally true and the constraints are effectively full UNIQUEs.

Step order inside ``upgrade()``:
    1. CREATE TABLE ``resources``
    2. Seed ``resources`` from active contexts (workspace_id, resource_id)
    3. Pre-migration audit: abort if any satellite row references a
       ``resource_id`` with no matching active context, which would
       leave ``resource_pk`` NULL permanently and defeat the audit.
    4. ADD COLUMN ``resource_pk`` nullable on four satellite tables
    5. Backfill ``resource_pk`` via JOIN on ``resources.resource_id``
       (kept nullable — see "PHASE 1 OF 2" above)
    6. Add FK + index on ``resource_pk`` per table
    7. ADD COLUMN ``resource_tokens.workspace_id`` nullable
    8. Backfill ``workspace_id`` from ``resources.workspace_id`` via
       the just-populated ``resource_pk``
    9. Add FK + index on ``resource_tokens.workspace_id``
    10. Create three partial UNIQUE indexes
        (``resource_events`` goes through CREATE INDEX CONCURRENTLY
        because it is the high-write satellite table)
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "a97_resources_entity"
down_revision = "a96_ctx_resource_id_unique"
branch_labels = None
depends_on = None


_MAX_AUDIT_EXAMPLES = 5

# Satellite tables that key themselves by resource_id today and will gain
# a resource_pk UUID FK column. Ordered for readable backfill logs.
_SATELLITE_TABLES = (
    "resource_events",
    "resource_schemas",
    "indexer_state",
    "resource_tokens",
)

_PARTIAL_UNIQUE_INDEX = "ux_resource_events_upsert_version"


def upgrade() -> None:
    """Normalize resource schema in a single migration (see module docstring)."""
    bind = op.get_bind()

    # --- Step 1: create the authoritative resources table -----------------
    op.create_table(
        "resources",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # ``resource_id`` gets a standalone non-unique index so the Step
        # 3 orphan audit and Step 5 backfill UPDATEs can resolve the
        # ``resource_id`` slug without a sequential scan. The composite
        # ``UNIQUE(workspace_id, resource_id)`` below cannot serve those
        # lookups efficiently because the leading column is
        # ``workspace_id``.
        sa.Column("resource_id", sa.String(255), nullable=False, index=True),
        # name / created_by are populated by later setup flows (issue #324+);
        # the migration cannot infer them from contexts alone.
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "resource_id",
            name="uq_resources_workspace_resource_id",
        ),
    )

    # --- Step 2: seed resources from ACTIVE contexts only -----------------
    # Scoping the seed to active, non-deleted contexts keeps
    # ``resources.resource_id`` globally unique (a96 enforces this
    # invariant for the active set) and guarantees the Step 5 backfill
    # JOIN resolves deterministically. Including soft-deleted contexts
    # would let a historical workspace's slug reappear in ``resources``
    # and silently cross-assign its satellite rows to a different
    # workspace that happens to share the slug — a tenancy boundary
    # break the whole refactor is meant to prevent. Satellite rows
    # whose owning context has been soft-deleted are handled by the
    # Step 3 audit (they are orphans that the operator must clean up).
    bind.execute(
        sa.text(
            "INSERT INTO resources (workspace_id, resource_id, created_by, created_at) "
            "SELECT DISTINCT workspace_id, resource_id, created_by, created_at "
            "FROM contexts "
            "WHERE resource_id IS NOT NULL AND deleted_at IS NULL"
        )
    )

    # --- Step 3: pre-migration audit (fail fast on orphans) ---------------
    # Any satellite row whose resource_id has no resources entry would
    # survive step 5 with resource_pk NULL and break step 6's NOT NULL
    # tightening. Raise with actionable examples so the operator can
    # clean up before rerunning.
    orphans: list[tuple[str, str, int]] = []
    for table in _SATELLITE_TABLES:
        result = bind.execute(
            sa.text(
                f"SELECT s.resource_id, COUNT(*) AS row_count "  # noqa: S608 -- table names are module-constant
                f"FROM {table} AS s "
                "LEFT JOIN resources r ON r.resource_id = s.resource_id "
                "WHERE r.id IS NULL "
                "GROUP BY s.resource_id "
                "LIMIT :limit"
            ),
            {"limit": _MAX_AUDIT_EXAMPLES},
        )
        for row in result.fetchall():
            orphans.append((table, row[0], row[1]))

    if orphans:
        examples = ", ".join(f"{tbl}.resource_id='{rid}' ({cnt} rows)" for tbl, rid, cnt in orphans)
        raise RuntimeError(
            "Migration aborted: satellite rows reference resource_id values that "
            f"have no matching active context (examples: {examples}). Either "
            "create the corresponding active contexts first or remove the "
            "orphaned rows before re-running this migration."
        )

    # --- Step 4-6: add resource_pk (nullable), backfill, add FK + index ---
    # resource_pk stays nullable so existing writers that only supply
    # resource_id keep working until application updates land (#324).
    # The follow-up migration (#325) tightens to NOT NULL.
    #
    # Index creation is split by table: the three low-write satellites
    # use a regular ``op.create_index`` (brief ACCESS EXCLUSIVE is fine
    # at their volume), while ``resource_events`` — the append-only
    # high-write log — gets its index built via
    # ``CREATE INDEX CONCURRENTLY`` further below to avoid blocking
    # writes during the build.
    for table in _SATELLITE_TABLES:
        op.add_column(
            table,
            sa.Column("resource_pk", UUID(as_uuid=True), nullable=True),
        )
        bind.execute(
            sa.text(
                # noqa: S608 -- table names are module-constant
                f"UPDATE {table} AS s "
                "SET resource_pk = r.id "
                "FROM resources AS r "
                "WHERE r.resource_id = s.resource_id"
            )
        )
        op.create_foreign_key(
            f"fk_{table}_resource_pk",
            table,
            "resources",
            ["resource_pk"],
            ["id"],
            ondelete="CASCADE",
        )
        if table != "resource_events":
            op.create_index(
                f"ix_{table}_resource_pk",
                table,
                ["resource_pk"],
            )

    # --- Step 7-9: resource_tokens.workspace_id shadow FK ----------------
    # Added separately from the resource_pk loop because the backfill
    # source is ``resources.workspace_id`` via the resource_pk populated
    # just above. Kept nullable for the same Phase 1 reason as
    # resource_pk — writers that do not yet know about workspace_id
    # must keep working until #324 updates them.
    op.add_column(
        "resource_tokens",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=True),
    )
    bind.execute(
        sa.text(
            "UPDATE resource_tokens AS t "
            "SET workspace_id = r.workspace_id "
            "FROM resources AS r "
            "WHERE r.id = t.resource_pk"
        )
    )
    op.create_foreign_key(
        "fk_resource_tokens_workspace",
        "resource_tokens",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_resource_tokens_workspace_id",
        "resource_tokens",
        ["workspace_id"],
    )

    # --- Step 10: pre-UNIQUE duplicate audit ------------------------------
    # Baseline never enforced these UNIQUEs, so the backfilled rows may
    # already violate them. Build the partial indexes only after proving
    # the existing data is clean; otherwise CREATE INDEX raises a
    # low-level ``duplicate key value violates unique constraint`` that
    # leaves the operator without an actionable hint. The audit mirrors
    # Step 3's orphan check pattern.
    unique_audits: tuple[tuple[str, str, str], ...] = (
        (
            "resource_schemas",
            "resource_pk, schema_version",
            "resource_pk IS NOT NULL",
        ),
        (
            "indexer_state",
            "resource_pk, context_id",
            "resource_pk IS NOT NULL",
        ),
        (
            "resource_events",
            "resource_pk, doc_id, version",
            "op = 'upsert' AND resource_pk IS NOT NULL",
        ),
    )
    duplicate_findings: list[tuple[str, str, int]] = []
    for table, columns, predicate in unique_audits:
        result = bind.execute(
            sa.text(
                # noqa: S608 -- audit query built from module-constant tuples
                f"SELECT {columns}, COUNT(*) AS dup_count "
                f"FROM {table} "
                f"WHERE {predicate} "
                f"GROUP BY {columns} "
                "HAVING COUNT(*) > 1 "
                "LIMIT :limit"
            ),
            {"limit": _MAX_AUDIT_EXAMPLES},
        )
        for row in result.fetchall():
            dup_key = ", ".join(str(v) for v in row[:-1])
            duplicate_findings.append((table, dup_key, row[-1]))

    if duplicate_findings:
        examples = ", ".join(f"{tbl}({keys})={cnt} rows" for tbl, keys, cnt in duplicate_findings)
        raise RuntimeError(
            "Migration aborted: duplicate rows exist that would violate the "
            f"Phase 1 partial UNIQUE indexes (examples: {examples}). These "
            "are pre-existing baseline duplicates — de-duplicate them via "
            "the usual DELETE/merge flow before re-running this migration."
        )

    # All three are partial on ``resource_pk IS NOT NULL`` so rows still
    # waiting for writer migration (resource_pk = NULL) do not collide
    # with each other and do not break ongoing traffic. Once #325
    # tightens resource_pk to NOT NULL, the predicate becomes universally
    # true and the indexes behave as full UNIQUEs.
    op.create_index(
        "uq_resource_schemas_version",
        "resource_schemas",
        ["resource_pk", "schema_version"],
        unique=True,
        postgresql_where=sa.text("resource_pk IS NOT NULL"),
    )
    op.create_index(
        "uq_indexer_state_resource_context",
        "indexer_state",
        ["resource_pk", "context_id"],
        unique=True,
        postgresql_where=sa.text("resource_pk IS NOT NULL"),
    )

    # resource_events is append-only and high-write, so the partial UNIQUE
    # goes through CREATE INDEX CONCURRENTLY. The predicate combines
    # ``op = 'upsert'`` (so delete events stay replayable — upsert →
    # delete → upsert revival is a valid invariant) with
    # ``resource_pk IS NOT NULL`` (Phase 1 semantics). Mirror a96's
    # INVALID-index guard for resilience against a prior failed
    # CONCURRENTLY run.
    invalid_index_result = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :index_name AND NOT i.indisvalid LIMIT 1"
        ),
        {"index_name": _PARTIAL_UNIQUE_INDEX},
    )
    has_invalid_index = invalid_index_result.fetchone() is not None

    with op.get_context().autocommit_block():
        if has_invalid_index:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_PARTIAL_UNIQUE_INDEX}"))
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_PARTIAL_UNIQUE_INDEX} "
                "ON resource_events (resource_pk, doc_id, version) "
                "WHERE op = 'upsert' AND resource_pk IS NOT NULL"
            )
        )
        # ``ix_resource_events_resource_pk`` is created concurrently for
        # the same reason as the partial UNIQUE: resource_events is the
        # high-write append-only log, so a blocking index build would
        # stall ingest traffic for the duration. IF NOT EXISTS lets this
        # path be safely re-run after a partial failure.
        op.execute(
            sa.text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_resource_events_resource_pk ON resource_events (resource_pk)"
            )
        )


def downgrade() -> None:
    """Reverse every upgrade step so the schema returns to a96 state."""
    # Drop the resource_events partial UNIQUE and non-unique resource_pk
    # index concurrently, mirroring how they were created.
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_PARTIAL_UNIQUE_INDEX}"))
        op.execute(sa.text("DROP INDEX CONCURRENTLY IF EXISTS ix_resource_events_resource_pk"))

    # Drop the two partial UNIQUE indexes (created via op.create_index,
    # not create_unique_constraint, so drop_index is the right inverse).
    op.drop_index("uq_indexer_state_resource_context", table_name="indexer_state")
    op.drop_index("uq_resource_schemas_version", table_name="resource_schemas")

    # resource_tokens.workspace_id teardown (reverse of step 7-9).
    op.drop_index("ix_resource_tokens_workspace_id", table_name="resource_tokens")
    op.drop_constraint("fk_resource_tokens_workspace", "resource_tokens", type_="foreignkey")
    op.drop_column("resource_tokens", "workspace_id")

    # resource_pk teardown on satellite tables (reverse of step 4-6).
    # resource_events' index was already dropped concurrently above, so
    # we skip drop_index for that table here.
    for table in reversed(_SATELLITE_TABLES):
        if table != "resource_events":
            op.drop_index(f"ix_{table}_resource_pk", table_name=table)
        op.drop_constraint(f"fk_{table}_resource_pk", table, type_="foreignkey")
        op.drop_column(table, "resource_pk")

    op.drop_table("resources")
