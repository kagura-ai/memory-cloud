"""Issue #390: orphan backfill + ambiguity audit for resource_pk (Phase 2 of 3).

Phase 1 (``a97_resources_entity``) backfilled ``resource_pk`` on rows that
existed at migration time and introduced the column as nullable. Phase 2
(this migration + application writer updates) populates ``resource_pk`` on
every new insert via SQLAlchemy ``before_insert`` event listeners; this
migration catches any orphan rows that were written between the a97 ship
date and the application writer ship date — rows where application code
set only ``resource_id`` (slug) while ``resource_pk`` silently defaulted
to NULL.

This migration is forward-only by intent: NULL → populated backfill is
trivially reversible (the column is still nullable), so a ``downgrade()``
to the pre-backfill state is a no-op. Phase C (``#325``) will tighten the
column to NOT NULL and drop the ``postgresql_where`` partial clause,
converting the partial UNIQUE indexes to full UNIQUE.

Revision ID: b01_resource_pk_ph2
Revises: a99_ext_keys_ws_not_null

BINDING CROSS-WORKSPACE AMBIGUITY AUDIT
---------------------------------------
Before attempting backfill, the migration scans for orphan rows whose
slug maps to more than one live ``resources`` row across workspaces. That
shape — which requires a soft-deleted workspace A resource sharing a slug
with a live workspace B resource — would cause an unqualified JOIN to
silently re-home workspace A's orphan satellite rows to workspace B,
reintroducing the same CWE-639 leak vector the writer migration is closing.
When detected, the migration aborts with an operator-actionable error
listing the offending slugs per table. In a solo-user deployment this is
expected to detect zero rows, but the audit itself is a shipped regression
guard (#390 design feedback, gate1 yellow → green after scope addendum).

TABLE-BY-TABLE BACKFILL STRATEGY
---------------------------------
- ``indexer_state`` has a ``context_id`` FK that is workspace-scoped via
  Context, so the backfill JOIN can safely use the Context →
  Resource correspondence to recover the correct ``resource_pk`` even
  under slug reuse. Safe to backfill row-by-row via a direct JOIN.
- ``resource_events``, ``resource_schemas``, ``resource_tokens`` have no
  ``context_id`` column. Without the ambiguity audit having passed,
  a slug-only JOIN could misattribute orphan rows. After the audit
  confirms the slug resolves to exactly one live workspace-scoped
  ``resources`` row, the JOIN is safe.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b01_resource_pk_ph2"
down_revision = "a99_ext_keys_ws_not_null"
branch_labels = None
depends_on = None


# Tables that need orphan backfill, classified by which column (if any) gives
# them a workspace-safe disambiguation path. The audit only runs on
# slug-only tables (the leftmost category). Tables with a usable
# disambiguation column skip the audit and use a workspace-scoped JOIN.
#
# - ``slug_only``: no workspace-scoped FK. Backfill via direct ``resources``
#   JOIN with ambiguity audit prerequisite. Used for ``resource_events``
#   and ``resource_schemas``.
# - ``has_context_id``: FK to ``contexts`` gives workspace via
#   ``contexts.workspace_id``. Backfill JOIN through contexts. Used for
#   ``indexer_state``.
# - ``has_workspace_id``: nullable shadow ``workspace_id`` FK (a97 Phase 1
#   backfilled existing rows; new writers populate it). When populated,
#   backfill via (workspace_id, resource_id) is safe even under slug reuse.
#   Rows with ``workspace_id IS NULL`` fall back to the slug-only path
#   with its ambiguity audit. Used for ``resource_tokens``.
_SATELLITE_TABLES_SLUG_ONLY = ("resource_events", "resource_schemas")
_SATELLITE_TABLES_HAS_WORKSPACE_ID = ("resource_tokens",)
# ``indexer_state`` (has_context_id) is handled inline by ``_backfill_indexer_state``;
# no constant tuple needed since it's a single-table special case.


def _audit_cross_workspace_ambiguity(conn: sa.Connection) -> None:
    """Abort the migration if any slug-only orphan maps to >1 live workspace.

    This is the canonical "0635c675 pattern" applied to Phase 2 orphan
    backfill. See the module docstring for the exploit shape being
    prevented. The audit runs ONLY on tables that have no workspace-safe
    disambiguation path — ``has_context_id`` and ``has_workspace_id``
    tables are workspace-safe by JOIN construction and auditing them
    would falsely abort whenever two workspaces legitimately share a slug
    (common case — slugs are per-workspace unique, not globally unique).
    ``resource_tokens`` additionally scopes the audit to
    ``workspace_id IS NULL`` rows, since rows with a populated
    ``workspace_id`` are workspace-safe via that column.
    """
    ambiguities: list[str] = []

    # Straight slug-only tables: resource_events, resource_schemas.
    # Any orphan row whose slug maps to >1 live Resource is ambiguous.
    for table_name in _SATELLITE_TABLES_SLUG_ONLY:
        # Table name is a module-constant; slug values never appear in the
        # f-string (only the whole-table scan does). Matches the a97
        # precedent (``# noqa: S608 -- table names are module-constant``).
        result = conn.execute(
            sa.text(
                f"""
                SELECT DISTINCT s.resource_id
                FROM {table_name} s
                WHERE s.resource_pk IS NULL
                  AND (
                    SELECT COUNT(*)
                    FROM resources r
                    WHERE r.resource_id = s.resource_id
                  ) > 1
                """  # noqa: S608 -- table names are module-constant
            )
        ).fetchall()
        if result:
            slugs = ", ".join(repr(row[0]) for row in result)
            ambiguities.append(f"{table_name}: {slugs}")

    # resource_tokens special case: only audit rows that lack workspace_id.
    # Rows with workspace_id populated can backfill via
    # (workspace_id, resource_id) JOIN without ambiguity risk.
    for table_name in _SATELLITE_TABLES_HAS_WORKSPACE_ID:
        result = conn.execute(
            sa.text(
                f"""
                SELECT DISTINCT s.resource_id
                FROM {table_name} s
                WHERE s.resource_pk IS NULL
                  AND s.workspace_id IS NULL
                  AND (
                    SELECT COUNT(*)
                    FROM resources r
                    WHERE r.resource_id = s.resource_id
                  ) > 1
                """  # noqa: S608 -- table names are module-constant
            )
        ).fetchall()
        if result:
            slugs = ", ".join(repr(row[0]) for row in result)
            ambiguities.append(f"{table_name} (workspace_id IS NULL): {slugs}")

    if ambiguities:
        lines = "\n  ".join(ambiguities)
        raise RuntimeError(
            "Phase 2 orphan backfill aborted: cross-workspace slug ambiguity "
            "detected. Manual resolution required before this migration can "
            "run.\n"
            f"  {lines}\n\n"
            "Each listed slug maps to >1 row in the ``resources`` table, which "
            "means a soft-deleted workspace's orphan satellite rows would be "
            "silently re-homed to another workspace by an unqualified JOIN. "
            "Inspect the ``resources`` and satellite tables for the offending "
            "slugs and either delete the orphan satellite rows or hard-delete "
            "the soft-deleted workspace before retrying."
        )


def _backfill_indexer_state(conn: sa.Connection) -> None:
    """Backfill ``indexer_state.resource_pk`` via (Context → Resource) JOIN.

    ``indexer_state.context_id`` FK gives us workspace scope even when the
    slug collides across workspaces — resolve via Context, then Resource.
    """
    conn.execute(
        sa.text(
            """
            UPDATE indexer_state s
            SET resource_pk = r.id
            FROM contexts c
            JOIN resources r
              ON r.workspace_id = c.workspace_id
             AND r.resource_id = c.resource_id
            WHERE s.resource_pk IS NULL
              AND s.context_id = c.id
            """
        )
    )


def _backfill_slug_only_table(conn: sa.Connection, table_name: str) -> None:
    """Backfill ``<satellite>.resource_pk`` via direct ``resources`` JOIN.

    Precondition: the cross-workspace ambiguity audit has confirmed every
    orphan slug maps to at most one ``resources`` row, so the JOIN is
    deterministic. Used for ``resource_events`` and ``resource_schemas``;
    ``resource_tokens`` has its own workspace-scoped path via
    ``_backfill_resource_tokens``.
    """
    conn.execute(
        sa.text(
            f"""
            UPDATE {table_name} s
            SET resource_pk = r.id
            FROM resources r
            WHERE s.resource_pk IS NULL
              AND r.resource_id = s.resource_id
            """  # noqa: S608 -- table_name is module-constant
        )
    )


def _backfill_resource_tokens(conn: sa.Connection) -> None:
    """Backfill ``resource_tokens.resource_pk`` in two passes.

    Pass 1 (workspace-safe): rows where ``workspace_id`` is populated
    JOIN on ``(workspace_id, resource_id)`` — this pins the correct
    Resource row even when the slug is globally reused across workspaces,
    so no audit prerequisite.

    Pass 2 (slug-only fallback): rows where ``workspace_id IS NULL``
    (pre-a97 legacy writes that predate the shadow column) can only JOIN
    on slug. These depend on the cross-workspace ambiguity audit having
    already passed.
    """
    # Pass 1: workspace-scoped JOIN — safe under slug reuse.
    conn.execute(
        sa.text(
            """
            UPDATE resource_tokens t
            SET resource_pk = r.id
            FROM resources r
            WHERE t.resource_pk IS NULL
              AND t.workspace_id IS NOT NULL
              AND r.workspace_id = t.workspace_id
              AND r.resource_id = t.resource_id
            """
        )
    )
    # Pass 2: slug-only for legacy rows; audit has already ruled out
    # ambiguity for workspace_id IS NULL + resource_pk IS NULL shape.
    conn.execute(
        sa.text(
            """
            UPDATE resource_tokens t
            SET resource_pk = r.id
            FROM resources r
            WHERE t.resource_pk IS NULL
              AND t.workspace_id IS NULL
              AND r.resource_id = t.resource_id
            """
        )
    )


def _backfill_resource_tokens_workspace_id(conn: sa.Connection) -> None:
    """Complete the a97 Phase 1 intent by backfilling ``workspace_id``.

    a97 backfilled ``resource_tokens.workspace_id`` from the at-rest
    ``resources.workspace_id`` JOIN once. Any token written between a97
    ship and this migration may still have ``workspace_id IS NULL``
    because the application writer for the token manager did not set it.
    This catches those — after this migration + the corresponding writer
    update, no new NULL rows should appear, which is what the Phase C
    NOT NULL tightening observation window verifies.
    """
    conn.execute(
        sa.text(
            """
            UPDATE resource_tokens t
            SET workspace_id = r.workspace_id
            FROM resources r
            WHERE t.workspace_id IS NULL
              AND t.resource_pk = r.id
            """
        )
    )


def _seed_missing_resources_from_active_contexts(conn: sa.Connection) -> None:
    """Ensure every active Context has a workspace-scoped ``resources`` row.

    a97 (Phase 1) seeded ``resources`` from the then-active contexts at
    migration time. Contexts created between a97 ship and the Phase 2
    writer update (``handle_setup_resource`` didn't start upserting
    ``Resource`` rows until this PR) can exist without a backing
    ``resources`` row. The ambiguity audit + slug-only backfill assume
    that invariant holds; if it doesn't, a workspace with an active
    Context but no ``resources`` row leaves its orphan satellite rows
    unmatched — and the slug-only backfill JOIN can silently re-home
    them to a different workspace's Resource that happens to share the
    slug. Reintroducing the CWE-639 leak this migration is supposed to
    close.

    Re-seed from the same shape a97 used, but with a ``NOT EXISTS``
    guard so existing rows are preserved. Copilot catch on PR #392
    loop 6 (re-entry).
    """
    conn.execute(
        sa.text(
            """
            INSERT INTO resources (workspace_id, resource_id)
            SELECT DISTINCT c.workspace_id, c.resource_id
            FROM contexts c
            WHERE c.deleted_at IS NULL
              AND c.resource_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM resources r
                WHERE r.workspace_id = c.workspace_id
                  AND r.resource_id = c.resource_id
              )
            """
        )
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Step 1: seed missing ``resources`` rows from active contexts. Ensures
    # every live (workspace_id, resource_id) has a Resource row BEFORE the
    # audit runs, so audit COUNT(resources) reflects the complete set.
    _seed_missing_resources_from_active_contexts(conn)

    # Step 2: abort if any slug-only orphan is ambiguous across workspaces.
    _audit_cross_workspace_ambiguity(conn)

    # Step 3: backfill the context-scoped table (safest JOIN shape).
    _backfill_indexer_state(conn)

    # Step 4: backfill resource_tokens (workspace-safe pass + slug-only fallback).
    _backfill_resource_tokens(conn)

    # Step 5: backfill the pure slug-only satellite tables (events, schemas).
    for table_name in _SATELLITE_TABLES_SLUG_ONLY:
        _backfill_slug_only_table(conn, table_name)

    # Step 6: finish a97 Phase 1's ``workspace_id`` backfill for any
    # newly-populated ``resource_pk`` values on resource_tokens.
    _backfill_resource_tokens_workspace_id(conn)


def downgrade() -> None:
    # Forward-only migration. NULL → populated is not reversible without
    # data loss (the NULL state cannot be reconstructed after the JOIN
    # has resolved ``resource_pk``), and Phase 1's schema still permits
    # NULL rows to coexist, so downgrade is a no-op.
    pass
