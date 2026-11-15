"""Join managed-feed and custom-domain histories without changing their data."""

revision = "f52_2808_merge_feed_domains"
down_revision = ("f51_2808_managed_sync", "f51_2809_public_mcp_domains")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply both unchanged parents before recording their common head."""
    pass


def downgrade() -> None:
    """Restore both parent markers without removing either parent's data."""
    pass
