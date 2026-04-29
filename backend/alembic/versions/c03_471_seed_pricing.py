"""Seed initial llm_pricing rows with 2026-04-28 provider rates.

Issue #471: separates pricing data from schema so a rollback / re-run can
target just the seed rows without dropping the table. Per
``CLAUDE.md`` and gate1 review (key suggestion #3), this is the
ALTER-friendly migration pattern preferred for billing-grade data.

Seeded rates (USD, all effective_from = ``2026-04-28T00:00:00Z``):

Anthropic (cache_write ≈ 1.25× input, cache_read ≈ 0.1× input):
- claude-opus-4-7   : input $5 / output $25 / cache_read $0.50 / cache_write $6.25 per 1M tokens
- claude-sonnet-4-6 : input $3 / output $15 / cache_read $0.30 / cache_write $3.75 per 1M
- claude-haiku-4-5  : input $1 / output $5 / cache_read $0.10 / cache_write $1.25 per 1M

OpenAI:
- gpt-5.5      : input $5 / output $30 / cache_read $0.50 per 1M tokens (no separate cache_write rate; OpenAI rolls write into input)
- gpt-5-nano   : input $0.20 / output $1.25 / cache_read $0.02 per 1M (the repo's default ``SLEEP_LLM_MODEL``; rates align with the gpt-5.4-nano tier)
- text-embedding-3-small : $0.02 per 1M tokens (this repo's default embedding model)
- text-embedding-3-large : $0.13 per 1M tokens

Ollama (local, free — embedding models registered in
``backend/src/config/constants.py`` ``EMBEDDING_MODEL_REGISTRY``):
- nomic-embed-text       : $0
- mxbai-embed-large      : $0
- qwen3-embedding:0.6b   : $0
- qwen3-embedding:4b     : $0
- qwen3-embedding:8b     : $0

Rerank pricing (Voyage / Cohere) is intentionally NOT seeded here —
``reranker_service.py`` is called only from the recall path
(``mcp_server/tools/memory.py:236``), which is outside Sleep
Maintenance scope. #474 (comprehensive ledger for non-Sleep paths)
seeds those rows when it ships.

Downgrade deletes ALL rows with this exact ``effective_from`` so a
re-seed of the same date doesn't pile up duplicates. If a price changes
later we INSERT a new row with a later ``effective_from`` rather than
mutating these seed rows — the lookup query picks the most recent row
whose ``effective_from <= started_at``.

Revision ID: c03_471_seed_pricing
Revises: c02_471_cost_grade_schema

NOTE: Revision ID is 21 chars (under the 32-char alembic limit).
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c03_471_seed_pricing"
down_revision: str | Sequence[str] | None = "c02_471_cost_grade_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Single source of truth for the seed effective_from. Used by both
# upgrade() (for INSERTs) and downgrade() (for the targeted DELETE).
# Naive UTC to match ``llm_pricing.effective_from`` (DateTime, naive)
# and the rest of the codebase's "store UTC, ignore tz metadata" pattern.
#
# MUST be a ``datetime`` not a ``str``: asyncpg infers bind types from the
# Python value, so a string would render as ``$N::VARCHAR`` and PostgreSQL
# refuses to coerce VARCHAR into TIMESTAMP WITHOUT TIME ZONE without an
# explicit cast — the upgrade INSERT (and the downgrade DELETE) blow up.
_SEED_EFFECTIVE_FROM = datetime(2026, 4, 28, 0, 0, 0)


# Parameterized INSERT — bind params keep the migration aligned with
# ``.claude/rules/security.md`` ("NEVER use f-strings for SQL"). Even
# though every seed value here is a hardcoded Python literal, the
# blanket no-f-string-SQL rule means we use bound params anyway so a
# future contributor doesn't copy-paste this pattern with an external
# variable and accidentally introduce an injection vector.
_INSERT_PRICING_SQL = sa.text("""
    INSERT INTO llm_pricing (
        provider, model, unit_type, effective_from,
        context_min_tokens, context_max_tokens,
        price_per_unit, currency, unit_denominator
    ) VALUES (
        :provider, :model, :unit_type, :effective_from,
        0, NULL,
        :price_per_unit, 'USD', 1000000
    )
""")


def _insert_pricing_row(
    *,
    provider: str,
    model: str,
    unit_type: str,
    price_per_unit: str,
) -> None:
    """Emit a single parameterized INSERT for an llm_pricing row.

    All seed rows share ``effective_from`` (`_SEED_EFFECTIVE_FROM`),
    ``unit_denominator=1000000``, ``currency='USD'``,
    ``context_min_tokens=0``, ``context_max_tokens=NULL``.

    Call sites pass ``price_per_unit`` as a string for literal-precision
    ergonomics (``"0.5"`` reads cleanly, no float repr quirk). It is
    converted to ``Decimal`` here before binding so asyncpg infers
    ``NUMERIC`` instead of ``VARCHAR`` — without the cast, PostgreSQL
    refuses to coerce VARCHAR into NUMERIC(14,10) at INSERT time and
    the migration aborts.
    """
    op.execute(
        _INSERT_PRICING_SQL.bindparams(
            provider=provider,
            model=model,
            unit_type=unit_type,
            effective_from=_SEED_EFFECTIVE_FROM,
            price_per_unit=Decimal(price_per_unit),
        )
    )


def upgrade() -> None:
    """Seed initial 2026-04-28 pricing rows."""
    # --- Anthropic Claude family (4 unit_types × 3 models = 12 rows) ---
    for model, rates in (
        ("claude-opus-4-7", ("5", "25", "0.5", "6.25")),
        ("claude-sonnet-4-6", ("3", "15", "0.3", "3.75")),
        ("claude-haiku-4-5", ("1", "5", "0.1", "1.25")),
    ):
        input_rate, output_rate, cache_read_rate, cache_write_rate = rates
        _insert_pricing_row(
            provider="anthropic", model=model, unit_type="input_tokens", price_per_unit=input_rate
        )
        _insert_pricing_row(
            provider="anthropic",
            model=model,
            unit_type="output_tokens",
            price_per_unit=output_rate,
        )
        _insert_pricing_row(
            provider="anthropic",
            model=model,
            unit_type="cache_read_tokens",
            price_per_unit=cache_read_rate,
        )
        _insert_pricing_row(
            provider="anthropic",
            model=model,
            unit_type="cache_write_tokens",
            price_per_unit=cache_write_rate,
        )

    # --- OpenAI LLM (gpt-5.5, gpt-5-nano: input/output/cache_read = 3 rows each) ---
    for model, rates in (
        ("gpt-5.5", ("5", "30", "0.5")),
        # gpt-5-nano = repo default; rates align with gpt-5.4-nano tier per
        # 2026-04-28 OpenAI pricing snapshot.
        ("gpt-5-nano", ("0.2", "1.25", "0.02")),
    ):
        input_rate, output_rate, cache_read_rate = rates
        _insert_pricing_row(
            provider="openai", model=model, unit_type="input_tokens", price_per_unit=input_rate
        )
        _insert_pricing_row(
            provider="openai", model=model, unit_type="output_tokens", price_per_unit=output_rate
        )
        _insert_pricing_row(
            provider="openai",
            model=model,
            unit_type="cache_read_tokens",
            price_per_unit=cache_read_rate,
        )

    # --- OpenAI embedding (this repo's defaults) ---
    _insert_pricing_row(
        provider="openai",
        model="text-embedding-3-small",
        unit_type="embedding_tokens",
        price_per_unit="0.02",
    )
    _insert_pricing_row(
        provider="openai",
        model="text-embedding-3-large",
        unit_type="embedding_tokens",
        price_per_unit="0.13",
    )

    # --- Ollama (local, free) — match EMBEDDING_MODEL_REGISTRY in constants.py ---
    for model in (
        "nomic-embed-text",
        "mxbai-embed-large",
        "qwen3-embedding:0.6b",
        "qwen3-embedding:4b",
        "qwen3-embedding:8b",
    ):
        _insert_pricing_row(
            provider="ollama",
            model=model,
            unit_type="embedding_tokens",
            price_per_unit="0",
        )


def downgrade() -> None:
    """Remove only the seed rows from this migration.

    Targeting by ``effective_from`` lets a price-change INSERT (with a
    later effective_from) survive a downgrade of just the initial seed.
    """
    op.execute(
        sa.text("DELETE FROM llm_pricing WHERE effective_from = :effective_from").bindparams(
            effective_from=_SEED_EFFECTIVE_FROM,
        )
    )
