"""Issue #396 AC 5: enforce workspace_id / context_id NOT NULL on neural_memory_edges.

Migration 062 added ``workspace_id`` and ``context_id`` to ``neural_memory_edges``
as nullable columns with a backfill for existing data, but the backfill was not
comprehensive for pre-062 rows whose endpoint memories themselves lacked those
fields (Memory got the isolation columns in migration 063). As a result, the
FK ``ON DELETE CASCADE`` on ``context_id`` is a death zone for NULL rows —
they survive context deletion, creating un-garbage-collectible orphans that
the GDPR right-to-erasure guarantee is supposed to cover. PR #394's read-path
Memory-scope filter masks them on SELECT, but they pile up.

This migration finishes the job from 062:

1. Backfill NULL ``workspace_id`` / ``context_id`` from endpoint memories,
   but ONLY when (a) src and dst memories agree on the (workspace_id,
   context_id) pair, AND (b) any pre-existing non-NULL value on the edge
   already matches the endpoint pair. Partially-populated edges whose
   non-NULL side disagrees with the endpoints are left NULL on purpose so
   Step 2 drops them as orphans — backfilling such rows would produce a
   fully-non-NULL row that violates the edge↔memory invariant and would
   then escape the orphan delete. Already fully-populated edges are
   skipped entirely by the ``IS NULL`` guard in the WHERE.
2. Delete truly orphan rows (no recoverable workspace/context on either
   endpoint) with a structured audit log capturing count + sample IDs.
3. Enforce ``NOT NULL`` using the zero-downtime ``NOT VALID`` → ``VALIDATE``
   → ``SET NOT NULL`` pattern (same shape a97_resources_entity uses for FKs
   on the high-write ``resource_events`` table): a CHECK constraint added
   ``NOT VALID`` skips the synchronous full-table scan during ``ALTER``,
   a ``VALIDATE CONSTRAINT`` proof under ``SHARE UPDATE EXCLUSIVE``
   afterwards, and finally ``SET NOT NULL`` which PG 12+ fast-paths since
   the CHECK is valid proof the column has no NULLs.

The CHECK constraint is kept after ``SET NOT NULL`` as belt-and-braces
alongside the sibling commit's write-path ``_validate_edge_context_invariant``
application guard: triple enforcement (app ValueError + CHECK + column
NOT NULL) is cheap, and dropping the CHECK would save nothing meaningful
while losing a second line of defense.

PRE-CONDITION: run ``backend/scripts/audit_edge_context_invariant.py --fix``
before applying this migration in any environment with legacy data, to clear
non-NULL invariant violations (edge.ctx != src_memory.ctx, etc.). That
script and this migration are complementary: audit handles the non-NULL
mismatches (AC 6), this migration handles the NULL-edge rows (AC 5).

Revision ID: b03_396_edges_ws_ctx_not_null
Revises: b02_383_edges_ws_ctx_idx
Create Date: 2026-04-20
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b03_396_edges_ws_ctx_not_null"
down_revision: str | Sequence[str] | None = "b02_383_edges_ws_ctx_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHECK_CONSTRAINT = "ck_neural_memory_edges_ws_ctx_not_null"

# Use the alembic logger so output goes through the standard migration
# logging config (see backend/alembic.ini) rather than bypassing it.
logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()

    # Step 1: backfill NULL workspace_id/context_id from endpoint memories.
    #
    # Two layers of guards protect the invariant:
    #
    # (a) ``ms.workspace_id = md.workspace_id AND ms.context_id = md.context_id``
    #     rejects edges whose src and dst memories live in different contexts.
    #     A naive ``COALESCE(e.ws, ms.ws, md.ws)`` would have silently picked
    #     one side and introduced the exact invariant violation the sibling
    #     write-path check (_validate_edge_context_invariant) exists to prevent.
    #     Those rows were already broken before 062 and have no legal
    #     resolution — leave them NULL so Step 2 drops them as orphans.
    #
    # (b) ``(e.workspace_id IS NULL OR e.workspace_id = ms.workspace_id)`` and
    #     the matching clause for ``context_id`` reject *partially*-populated
    #     edges whose non-NULL side disagrees with the endpoint memories.
    #     Without this guard, an edge with e.ws=X (non-NULL but inconsistent)
    #     and e.ctx=NULL would get COALESCE-filled on ctx only, producing a
    #     fully-non-NULL row that (i) escapes the Step 2 orphan delete and
    #     (ii) still violates the invariant. Leaving the partial-NULL row
    #     NULL keeps it in the orphan delete's scope, which is the right
    #     failure mode.
    #
    # The guards above also implicitly filter out cases where either endpoint
    # memory still has NULL ws/ctx (pre-063 data migration 063 did not reach)
    # — the equality-on-NULL predicates are false, so those edges fall through
    # to the orphan delete as well.
    backfill_result = conn.execute(
        sa.text(
            """
            UPDATE neural_memory_edges AS e
            SET workspace_id = COALESCE(e.workspace_id, ms.workspace_id),
                context_id   = COALESCE(e.context_id,   ms.context_id)
            FROM memories AS ms, memories AS md
            WHERE ms.id = e.src_id
              AND md.id = e.dst_id
              AND (e.workspace_id IS NULL OR e.context_id IS NULL)
              AND ms.workspace_id IS NOT NULL
              AND ms.context_id   IS NOT NULL
              AND ms.workspace_id = md.workspace_id
              AND ms.context_id   = md.context_id
              AND (e.workspace_id IS NULL OR e.workspace_id = ms.workspace_id)
              AND (e.context_id   IS NULL OR e.context_id   = ms.context_id)
            """
        )
    )
    logger.info(
        "[#396] backfilled %s edge row(s) from endpoint memories",
        backfill_result.rowcount or 0,
    )

    # Step 2: delete truly orphan rows. Three shapes remain NULL after Step 1:
    #   (a) edge whose src or dst memory row is gone,
    #   (b) edge whose endpoint memories both have NULL ws/ctx themselves
    #       (pre-063 memories the later backfill also missed),
    #   (c) edge that was created in an unreachable state and never got a
    #       workspace/context assigned.
    # All three are un-GC-able by the FK CASCADE and cannot be rescued; drop
    # them and log enough to investigate later if volume is surprising.
    orphan_rows = conn.execute(
        sa.text(
            "SELECT id, user_id, src_id, dst_id, workspace_id, context_id, edge_type "
            "FROM neural_memory_edges "
            "WHERE workspace_id IS NULL OR context_id IS NULL "
            "LIMIT 20"
        )
    ).fetchall()
    orphan_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM neural_memory_edges "
            "WHERE workspace_id IS NULL OR context_id IS NULL"
        )
    ).scalar()
    if orphan_count and orphan_count > 0:
        sample = ", ".join(
            f"id={row[0]} user={row[1]} src={row[2]} dst={row[3]} type={row[6]}"
            for row in orphan_rows
        )
        logger.warning(
            "[#396] deleting %s orphan edge row(s) "
            "(no recoverable workspace/context on either endpoint). Sample: %s",
            orphan_count,
            sample,
        )
        conn.execute(
            sa.text(
                "DELETE FROM neural_memory_edges WHERE workspace_id IS NULL OR context_id IS NULL"
            )
        )

    # Step 3a: add CHECK constraint NOT VALID. Skips synchronous scan; new
    # writes are checked immediately, only pre-existing rows need the
    # subsequent VALIDATE pass.
    op.execute(
        sa.text(
            f"ALTER TABLE neural_memory_edges "
            f"ADD CONSTRAINT {_CHECK_CONSTRAINT} "
            "CHECK (workspace_id IS NOT NULL AND context_id IS NOT NULL) NOT VALID"
        )
    )

    # Step 3b: VALIDATE under SHARE UPDATE EXCLUSIVE (does not block reads/writes).
    op.execute(sa.text(f"ALTER TABLE neural_memory_edges VALIDATE CONSTRAINT {_CHECK_CONSTRAINT}"))

    # Step 3c: SET NOT NULL — PG 12+ uses the validated CHECK as proof that
    # the column has no NULLs, skipping the synchronous full-table scan.
    # Brief ACCESS EXCLUSIVE to flip the catalog bit.
    op.alter_column(
        "neural_memory_edges",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "neural_memory_edges",
        "context_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )


def downgrade() -> None:
    # Reverse of Step 3c → 3b/3a. The backfilled data and orphan deletion in
    # Steps 1-2 are NOT reversed — reviving deleted edges from an audit log
    # is not useful, and re-nulling backfilled values would undo correct data.
    op.alter_column(
        "neural_memory_edges",
        "context_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "neural_memory_edges",
        "workspace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.execute(
        sa.text(f"ALTER TABLE neural_memory_edges DROP CONSTRAINT IF EXISTS {_CHECK_CONSTRAINT}")
    )
