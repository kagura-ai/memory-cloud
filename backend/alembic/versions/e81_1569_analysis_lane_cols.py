"""#1569: record the LLM lane on ``memory_analyses``; ``model_id`` nullable.

Memory Analysis may now run on the deployment's platform-managed LLM
(``MANAGED_LLM_PROVIDER`` / ``MANAGED_LLM_MODEL``) instead of a workspace's
BYOK OpenAI key. That model need not have an ``llm_pricing`` row — cost is
then "unknown" (#1570 semantics), so the ``model_id`` FK becomes nullable
instead of blocking the run. Two String columns record what actually ran so a
row without a pricing FK still says which provider/model labelled it:

- ``llm_provider`` (String(50)) — ``LLMService`` provider key.
- ``llm_model`` (String(100)) — primary model of the run's chain.

Both are NULL on rows written before this revision (all BYOK on the OpenAI
chain); ``services.analysis.llm_lane.lane_for_run`` treats NULL as BYOK.

Downgrade refuses while any row has ``model_id IS NULL`` (restoring NOT NULL
would fail on them; the operator decides what to do with unpriced runs), then
drops the two columns and restores NOT NULL.

Revision ID: e81_1569_analysis_lane_cols
Revises: e80_1570_unseed_sh_zero_pricing

Note: revision IDs must stay <= 32 chars — ``alembic_version.version_num``
is varchar(32).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e81_1569_analysis_lane_cols"
down_revision: str | None = "e80_1570_unseed_sh_zero_pricing"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Relax ``model_id`` and add the lane record columns."""
    op.alter_column(
        "memory_analyses",
        "model_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.add_column("memory_analyses", sa.Column("llm_provider", sa.String(50), nullable=True))
    op.add_column("memory_analyses", sa.Column("llm_model", sa.String(100), nullable=True))


def downgrade() -> None:
    """Drop the lane columns and restore NOT NULL — only when no unpriced runs exist."""
    null_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM memory_analyses WHERE model_id IS NULL"))
        .scalar_one()
    )
    if null_rows:
        raise RuntimeError(
            f"Cannot downgrade e81_1569_analysis_lane_cols: {null_rows} memory_analyses "
            "row(s) have model_id IS NULL (managed-lane runs without a pricing row). "
            "Delete them or point them at an llm_pricing row first."
        )
    op.drop_column("memory_analyses", "llm_model")
    op.drop_column("memory_analyses", "llm_provider")
    op.alter_column(
        "memory_analyses",
        "model_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
