"""#1570: drop the seeded $0 ``self_hosted`` embedding prices — unknown, not free.

``c03_471_seed_pricing`` seeded the registry's five self-hosted embedding
models at ``price_per_unit = 0`` (as ``provider='ollama'``; ``e53_1160``
renamed them to ``self_hosted``). That modelled a local Ollama. ``self_hosted``
is now also how a *paid* OpenAI-compatible endpoint is wired, and for that
deployment ``$0.00`` on the cost dashboard is worse than "unknown": the price
is simply not known to the platform until the operator sets it.

After this revision an unpriced self-hosted model resolves to *no* row:
``compute_cost_usd`` → ``None`` (cost unknown, ``—`` in the dashboard, spend
cap inert — the pre-#1570 behaviour), and ``llm_call_log`` marks
``pricing_miss``. An operator sets a real price with ``LLM_PRICING_OVERRIDES``;
one who wants an explicit ``$0.00`` for a truly free local model sets an
override at price 0.

Only the exact seed rows are removed (the five ``_SEED_MODELS`` names,
``effective_from = 2026-04-28``, price 0) — an operator-added ``self_hosted``
row that happens to share the timestamp and price is left alone. A row still
referenced by an FK (``memory_analyses.model_id`` RESTRICT,
``workspaces.analysis_default_model_id`` / ``analysis_quality_model_id`` SET
NULL) is kept and logged rather than deleted, so no analysis history loses its
snapshot row and no workspace default is silently nulled.

Downgrade re-inserts the five rows at price 0 (``ON CONFLICT DO NOTHING`` on
``uq_llm_pricing_lookup_key``, so a preserved row is not duplicated).

Revision ID: e80_1570_unseed_sh_zero_pricing
Revises: e79_1548_promax_plan_tier
"""

import logging
from datetime import datetime

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e80_1570_unseed_sh_zero_pricing"
down_revision: str | None = "e79_1548_promax_plan_tier"
branch_labels: str | None = None
depends_on: str | None = None

# Use the alembic logger so output goes through the standard migration
# logging pipeline.
logger = logging.getLogger("alembic.runtime.migration")

# The c03 seed's effective_from (naive UTC, matching the ``DateTime`` column —
# hence no tzinfo). MUST stay a ``datetime`` so the bind renders as TIMESTAMP,
# not VARCHAR.
_SEED_EFFECTIVE_FROM = datetime(2026, 4, 28, 0, 0, 0)  # noqa: DTZ001

# Mirrors the c03 seed list (= ``EMBEDDING_MODEL_REGISTRY`` self-hosted models).
_SEED_MODELS = (
    "nomic-embed-text",
    "mxbai-embed-large",
    "qwen3-embedding:0.6b",
    "qwen3-embedding:4b",
    "qwen3-embedding:8b",
)

# Every FK onto ``llm_pricing.id`` (grep ``ForeignKey("llm_pricing.id"`` in
# ``models/``). Extend this when a new one lands, or the DELETE below either
# fails (RESTRICT) or silently nulls a column (SET NULL).
_REFERENCED = (
    "EXISTS (SELECT 1 FROM memory_analyses a WHERE a.model_id = p.id)"
    " OR EXISTS (SELECT 1 FROM workspaces w"
    "           WHERE w.analysis_default_model_id = p.id"
    "              OR w.analysis_quality_model_id = p.id)"
)

# The seed rows: bound ``:effective_from`` and the ``:model_N`` names are the
# only runtime values; every other fragment is a constant string assembled at
# import time.
_SEED_MODEL_PARAMS = {f"model_{i}": model for i, model in enumerate(_SEED_MODELS)}
_SEED_ROWS_WHERE = (
    "p.provider = 'self_hosted'"
    " AND p.unit_type = 'embedding_tokens'"
    " AND p.price_per_unit = 0"
    " AND p.effective_from = :effective_from"
    " AND p.model IN (" + ", ".join(f":{name}" for name in _SEED_MODEL_PARAMS) + ")"
)
_SEED_ROWS_PARAMS = {"effective_from": _SEED_EFFECTIVE_FROM, **_SEED_MODEL_PARAMS}

_SELECT_REFERENCED_SQL = (
    "SELECT p.id, p.model FROM llm_pricing p WHERE "
    + _SEED_ROWS_WHERE
    + " AND ("
    + _REFERENCED
    + ")"
)
_DELETE_UNREFERENCED_SQL = (
    "DELETE FROM llm_pricing p WHERE " + _SEED_ROWS_WHERE + " AND NOT (" + _REFERENCED + ")"
)


def upgrade() -> None:
    """Delete the unreferenced $0 self_hosted embedding seed rows."""
    bind = op.get_bind()
    referenced = bind.execute(text(_SELECT_REFERENCED_SQL), _SEED_ROWS_PARAMS).fetchall()
    for row in referenced:
        logger.warning(
            "e80_1570: keeping $0 self_hosted pricing row id=%s model=%s — still referenced "
            "by memory_analyses.model_id or a workspace analysis default",
            row.id,
            row.model,
        )
    deleted = bind.execute(text(_DELETE_UNREFERENCED_SQL), _SEED_ROWS_PARAMS).rowcount
    logger.info(
        "e80_1570: removed %s seeded $0 self_hosted embedding pricing row(s); kept %s referenced",
        deleted,
        len(referenced),
    )


def downgrade() -> None:
    """Re-insert the five $0 rows (idempotent on the lookup-key unique constraint)."""
    bind = op.get_bind()
    for model in _SEED_MODELS:
        bind.execute(
            text(
                "INSERT INTO llm_pricing (provider, model, unit_type, effective_from,"
                " context_min_tokens, context_max_tokens, pricing_model, price_per_unit,"
                " currency, unit_denominator)"
                " VALUES ('self_hosted', :model, 'embedding_tokens', :effective_from,"
                " 0, NULL, 'per_token', 0, 'USD', 1000000)"
                " ON CONFLICT ON CONSTRAINT uq_llm_pricing_lookup_key DO NOTHING"
            ),
            {"model": model, "effective_from": _SEED_EFFECTIVE_FROM},
        )
