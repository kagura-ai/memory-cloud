"""Self-service MFA enrollment and single-use recovery codes (#2644)."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "f25_2644_mfa_recovery"
down_revision = "f24_2650_attachment_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_generation", sa.String(32)))
    op.add_column("users", sa.Column("totp_recovery_hashes", JSONB()))


def downgrade() -> None:
    op.drop_column("users", "totp_recovery_hashes")
    op.drop_column("users", "mfa_generation")
