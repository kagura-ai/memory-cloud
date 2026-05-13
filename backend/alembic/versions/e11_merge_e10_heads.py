"""Merge e10 heads (kagura-cli client + apikey context binding).

#624 (``e10_624_seed_kagura_cli_client``) and #626
(``e10_626_apikey_bound_context_id``) both branched from
``e09_608_dcr_default_narrow``. They were merged to ``main`` in sequence
without rebasing the second migration's ``down_revision``, leaving the
alembic chain with two heads. This empty merge revision converges them
into a single head so ``alembic upgrade head`` succeeds in production.

No schema changes here — both branches are independently safe and
already applied in dev. This is purely a topology fix.
"""

revision = "e11_merge_e10_heads"
down_revision = ("e10_624_seed_kagura_cli_client", "e10_626_apikey_bound_context_id")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
