"""Join staged-feed and public-budget migrations without rewriting either branch."""

revision = "f50_2807_merge_sync_heads"
down_revision = ("f48_2807_sync_feed_runs", "f49_2817_clear_allowlist_adders")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parent migrations are applied by Alembic before this no-op merge."""
    pass


def downgrade() -> None:
    """Unmerge version markers; keep both parents' schemas and data intact."""
    pass
