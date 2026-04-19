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


# Tables that need orphan backfill. ``has_context_id`` controls whether the
# backfill JOIN can use ``context_id`` for workspace disambiguation.
_SATELLITE_TABLES = [
    ("resource_events", False),
    ("resource_schemas", False),
    ("indexer_state", True),
    ("resource_tokens", False),
]


def _audit_cross_workspace_ambiguity(conn: sa.Connection) -> None:
    """Abort the migration if any satellite slug maps to >1 live workspace.

    This is the canonical "0635c675 pattern" applied to Phase 2 orphan
    backfill. See the module docstring for the exploit shape being
    prevented.
    """
    ambiguities: list[str] = []
    # The audit runs on all 4 satellite tables regardless of context_id
    # presence, so unpack just the name.
    for table_name, _ in _SATELLITE_TABLES:
        # Table name is a module-constant from ``_SATELLITE_TABLES``; slug
        # values never appear in the f-string (only the whole-table scan
        # does). Matches the a97 precedent (``# noqa: S608 -- table names
        # are module-constant``) for the same shape.
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
    deterministic. ``resource_events``, ``resource_schemas``, and
    ``resource_tokens`` all take this path.
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


def upgrade() -> None:
    conn = op.get_bind()

    # Step 1: abort if any orphan slug is ambiguous across workspaces.
    _audit_cross_workspace_ambiguity(conn)

    # Step 2: backfill the context-scoped table first (safest JOIN shape).
    _backfill_indexer_state(conn)

    # Step 3: backfill the three slug-only satellite tables.
    for table_name, has_context_id in _SATELLITE_TABLES:
        if has_context_id:
            continue  # indexer_state handled above
        _backfill_slug_only_table(conn, table_name)

    # Step 4: finish a97 Phase 1's ``workspace_id`` backfill for any
    # newly-populated ``resource_pk`` values on resource_tokens.
    _backfill_resource_tokens_workspace_id(conn)


def downgrade() -> None:
    # Forward-only migration. NULL → populated is not reversible without
    # data loss (the NULL state cannot be reconstructed after the JOIN
    # has resolved ``resource_pk``), and Phase 1's schema still permits
    # NULL rows to coexist, so downgrade is a no-op.
    pass
