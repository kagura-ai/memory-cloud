"""#1548: admit the XL ("Pro Max", key ``promax``) tier in ``valid_plan_name``.

``workspaces.plan_name`` is guarded by a CHECK that enumerates the tier keys.
Without this revision a workspace pushed to ``promax`` by the admin plan API or
the billing entitlement push fails at the database, after the registry, rate
limits and endpoints already accept it.

Downgrade restores the three-key CHECK, and refuses to run while any row still
uses ``promax`` — re-creating the narrower constraint would fail anyway, and a
silent data rewrite is worse than a loud stop.

Revision ID: e79_1548_promax_plan_tier
Revises: e78_1523_repin_scope
"""

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e79_1548_promax_plan_tier"
down_revision: str | None = "e78_1523_repin_scope"
branch_labels: str | None = None
depends_on: str | None = None

# Mirrors ``models.auth.Workspace.__table_args__`` byte-for-byte (schema drift test).
CK_VALID_PLAN_NAME = "plan_name IN ('free', 'basic', 'pro', 'promax')"
CK_VALID_PLAN_NAME_PREV = "plan_name IN ('free', 'basic', 'pro')"


def upgrade() -> None:
    """Widen the CHECK to the four registered tiers."""
    op.drop_constraint("valid_plan_name", "workspaces", type_="check")
    op.create_check_constraint("valid_plan_name", "workspaces", CK_VALID_PLAN_NAME)


def downgrade() -> None:
    """Restore the three-tier CHECK; fail loudly if any workspace is on ``promax``."""
    promax_rows = (
        op.get_bind()
        .execute(text("SELECT count(*) FROM workspaces WHERE plan_name = 'promax'"))
        .scalar_one()
    )
    if promax_rows:
        raise RuntimeError(
            f"{promax_rows} workspace(s) are on plan 'promax'; move them to another tier "
            "before downgrading e79_1548_promax_plan_tier"
        )
    op.drop_constraint("valid_plan_name", "workspaces", type_="check")
    op.create_check_constraint("valid_plan_name", "workspaces", CK_VALID_PLAN_NAME_PREV)
