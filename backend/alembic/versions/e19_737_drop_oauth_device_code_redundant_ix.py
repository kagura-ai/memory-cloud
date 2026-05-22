"""Drop redundant non-unique indexes on oauth_device_codes (Issue #737).

The ``unique=True`` declarations on ``device_code`` and ``user_code`` already
materialize auto-managed unique backing indexes (``*_key``). The pair of
non-unique ``ix_*`` indexes installed by ``d08_536_device_code_grant.py``
duplicates that coverage and gets no traffic — pure write-amplification
waste. This migration removes them; the ORM-side ``Index(...)`` mirror
lines in ``OAuth2DeviceCode.__table_args__`` are removed in the same PR
to keep ``create_all`` byte-parity with alembic head.

Revision ID: e19_737_drop_redundant_ix
Revises: e18_616_drop_pg_inline
Create Date: 2026-05-22

Note: revision ID shortened from ``e19_737_drop_oauth_device_code_redundant_ix``
(43 chars) to ``e19_737_drop_redundant_ix`` (25 chars) to fit the
``alembic_version.version_num`` ``VARCHAR(32)`` column. The filename keeps
the longer descriptive form for grep-ability.
"""

from alembic import op


revision = "e19_737_drop_redundant_ix"
down_revision = "e18_616_drop_pg_inline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop both redundant non-unique indexes.

    The unique backing indexes (``oauth_device_codes_device_code_key`` and
    ``oauth_device_codes_user_code_key``) remain in place and continue
    covering every equality lookup these dropped indexes were serving.
    """
    op.drop_index(
        "ix_oauth_device_codes_device_code",
        table_name="oauth_device_codes",
    )
    op.drop_index(
        "ix_oauth_device_codes_user_code",
        table_name="oauth_device_codes",
    )


def downgrade() -> None:
    """Re-create the two indexes with their original non-unique semantics."""
    op.create_index(
        "ix_oauth_device_codes_device_code",
        "oauth_device_codes",
        ["device_code"],
        unique=False,
    )
    op.create_index(
        "ix_oauth_device_codes_user_code",
        "oauth_device_codes",
        ["user_code"],
        unique=False,
    )
