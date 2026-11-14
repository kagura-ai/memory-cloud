"""Reconcile sync credentials with the notice/admission branch (#2819).

Each parent keeps its original history. Upgrading either branch applies the
missing branch before this no-DDL merge; downgrading this node keeps both
schemas and their data while restoring the two parent version markers.
"""

revision: str = "f47_2819_merge_sync_credentials"
down_revision: tuple[str, str] = (
    "f45_2806_sync_credentials",
    "f46_2803_admission_record",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the two applied histories without changing their schemas."""


def downgrade() -> None:
    """Restore both parent markers without dropping either schema."""
