"""api_keys: agent_id IS NOT NULL implies workspace_id IS NOT NULL (#1281 item 4).

Structural backstop for the RFC-0002 P0-2 invariant that an agent-bound member
key is always workspace-scoped. It was already enforced at mint (service layer)
and re-asserted fail-closed at verify; this makes it a single-table DB CHECK so
the guarantee cannot drift on a direct write or a future code path.

Added ``NOT VALID`` (no scan under lock) then ``VALIDATE`` as a separate
statement (SHARE UPDATE EXCLUSIVE — does not block reads/writes), mirroring the
e65 ``ck_api_keys_agent_public_exclusion`` addition. Fresh feature: no
pre-existing rows can violate it (every minted agent key already carries
``workspace_id``), so VALIDATE is a formality that also guards against a bad
backfill.

Rerun safety (#655 pattern): the ADD is a ``duplicate_object``-guarded DO block;
VALIDATE runs in an autocommit block and is a no-op once validated.
"""

import sqlalchemy as sa

from alembic import op

revision = "e67_1281_agent_ws"
down_revision = "e66_1278_mae"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_api_keys_agent_requires_workspace"


def upgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"  ALTER TABLE api_keys ADD CONSTRAINT {_CHECK_NAME} "
            "    CHECK (agent_id IS NULL OR workspace_id IS NOT NULL) NOT VALID; "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        )
    )
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"ALTER TABLE api_keys VALIDATE CONSTRAINT {_CHECK_NAME}"))


def downgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS {_CHECK_NAME}"))
