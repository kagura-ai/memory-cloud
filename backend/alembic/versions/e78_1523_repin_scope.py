"""#1523: pinned rows live in the persistent scope — repair the ones that do not.

``_apply_pin_on_write`` makes every pinned write (``delivery_mode='always'``)
persistent, so the deterministic ``load_pinned()`` lane never depends on the
consolidation pass. One producer could still leave a pinned row in the
working scope: ``rollback_sleep_run`` demoting a promotion that was pinned
after the run. From this release that rollback refuses pinned rows, and the
Sleep candidate fetches exclude them, so a pinned working row would otherwise
sit outside every pass forever — never promoted, never archived. Move the
existing ones where pin-on-write would have put them.

Downgrade is a no-op: nothing records which rows were working before.

Revision ID: e78_1523_repin_scope
Revises: e77_1496_embed_retry_backfill
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e78_1523_repin_scope"
down_revision: str | None = "e77_1496_embed_retry_backfill"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Give live pinned working rows the persistent scope pin-on-write implies."""
    op.execute(
        """
        UPDATE memories
        SET scope = 'persistent',
            promoted_at = COALESCE(promoted_at, NOW())
        WHERE delivery_mode = 'always'
          AND scope = 'working'
          AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    """No-op: the pre-migration scope is not recorded."""
