"""Seed llm_pricing rows for Voyage rerank + Cohere rerank/embed (#474).

Issue #474: ``llm_pricing`` was seeded by ``c03_471_seed_pricing`` with
Sleep-relevant LLM and embedding rates only — rerank pricing was
intentionally deferred (see that file's header). This migration closes
that gap by adding the four provider/model rows recall reranking
(#475 PR-3) and any future non-Sleep recall instrumentation will look
up:

- ``voyage`` × ``rerank-2`` × ``rerank_tokens`` — $0.05 / 1M tokens
- ``voyage`` × ``rerank-2-lite`` × ``rerank_tokens`` — $0.02 / 1M tokens
- ``cohere`` × ``rerank-3.5`` × ``rerank_search_units`` — $2.00 / 1k SU
  (Cohere splits docs > 500 tokens into multiple SUs — captured by the
  reranker_service from ``response.meta.billed_units.search_units``
  when #475 PR-3 wires up the writer)
- ``cohere`` × ``embed-multilingual-v3`` × ``embedding_tokens`` —
  $0.10 / 1M tokens

All effective_from = 2026-04-28 to align with c03 seeds (the canonical
snapshot date). The append-only contract means a price change inserts
a new row with a later ``effective_from`` — these seed rows are never
mutated.

Schema-vs-data split rationale: ``e12_474_llm_call_log`` adds the
event-log table itself; this revision is data-fixup. Splitting them
keeps the rollback unit clean — a price correction needing a re-seed
doesn't have to drop the table.

NOTE: Rates above are the public 2026-04-28 snapshot. Corrections land
as new INSERT rows with later ``effective_from``; downgrade here only
removes rows with this exact ``effective_from`` so corrections survive
a partial rollback.

Revision ID: e13_474_pricing_seeds
Revises: e12_474_llm_call_log

NOTE: Revision ID is 21 chars (under the 25-char asyncpg-safe limit).
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e13_474_pricing_seeds"
down_revision: str | Sequence[str] | None = "e12_474_llm_call_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Aligned with c03_471_seed_pricing's snapshot date so both seed sets
# share a single canonical ``effective_from`` — recall path lookups
# don't have to compose two different snapshots.
_SEED_EFFECTIVE_FROM = datetime(2026, 4, 28, 0, 0, 0)


# Parameterized INSERT supporting both unit_denominator values
# (1_000_000 for per-1M-token rates, 1_000 for per-1k-SU Cohere rates).
# Mirrors c03 style: bound params per `.claude/rules/security.md`
# (no f-string SQL even with hardcoded literals).
_INSERT_PRICING_SQL = sa.text("""
    INSERT INTO llm_pricing (
        provider, model, unit_type, effective_from,
        context_min_tokens, context_max_tokens,
        price_per_unit, currency, unit_denominator
    ) VALUES (
        :provider, :model, :unit_type, :effective_from,
        0, NULL,
        :price_per_unit, 'USD', :unit_denominator
    )
""")


def _insert_pricing_row(
    *,
    provider: str,
    model: str,
    unit_type: str,
    price_per_unit: str,
    unit_denominator: int,
) -> None:
    """Emit one parameterized INSERT against llm_pricing.

    ``price_per_unit`` is taken as a string for literal-precision
    ergonomics, converted to ``Decimal`` before binding so asyncpg
    infers ``NUMERIC`` (a VARCHAR bind would force PostgreSQL into a
    coercion failure against ``NUMERIC(14, 10)``). ``unit_denominator``
    is exposed so Cohere's per-1k-SU rows fit the same code path as
    Voyage's per-1M-token rows.
    """
    op.execute(
        _INSERT_PRICING_SQL.bindparams(
            provider=provider,
            model=model,
            unit_type=unit_type,
            effective_from=_SEED_EFFECTIVE_FROM,
            price_per_unit=Decimal(price_per_unit),
            unit_denominator=unit_denominator,
        )
    )


def upgrade() -> None:
    """Seed Voyage rerank + Cohere rerank/embed rows for 2026-04-28.

    Iterates ``_SEED_ROWS`` so the upgrade and downgrade stay locked to
    the same authoritative list. Rate semantics:

    - Voyage rerank-2 / rerank-2-lite: token-based, per 1M tokens.
    - Cohere rerank-3.5: per "search unit", $2.00 per 1k SU
      (``unit_denominator=1_000`` puts it on the same row shape as the
      token-based rows; the cost formula uses ``unit_denominator`` as
      the divisor uniformly).
    - Cohere embed-multilingual-v3: token-based, per 1M tokens (aligns
      with the OpenAI embedding rows c03 seeded).
    """
    for provider, model, unit_type, price_per_unit, unit_denominator in _SEED_ROWS:
        _insert_pricing_row(
            provider=provider,
            model=model,
            unit_type=unit_type,
            price_per_unit=price_per_unit,
            unit_denominator=unit_denominator,
        )


_DELETE_PRICING_SQL = sa.text("""
    DELETE FROM llm_pricing
    WHERE provider = :provider
      AND model = :model
      AND unit_type = :unit_type
      AND effective_from = :effective_from
""")


# Authoritative list of the exact (provider, model, unit_type) tuples
# this migration inserts. ``upgrade()`` and ``downgrade()`` both iterate
# this so the two stay locked together — a future contributor who adds
# a fifth seed row only edits one place.
_SEED_ROWS: tuple[tuple[str, str, str, str, int], ...] = (
    # (provider, model, unit_type, price_per_unit, unit_denominator)
    ("voyage", "rerank-2", "rerank_tokens", "0.05", 1_000_000),
    ("voyage", "rerank-2-lite", "rerank_tokens", "0.02", 1_000_000),
    ("cohere", "rerank-3.5", "rerank_search_units", "2.00", 1_000),
    ("cohere", "embed-multilingual-v3", "embedding_tokens", "0.10", 1_000_000),
)


def downgrade() -> None:
    """Remove only the seed rows this migration inserted.

    Targets each row by its exact ``(provider, model, unit_type,
    effective_from)`` tuple, so a future price correction INSERTed with
    a later ``effective_from`` AND any unrelated row added by a future
    migration at the same ``effective_from`` (e.g., a new Voyage / Cohere
    model rate that shares 2026-04-28 as its snapshot) survives a partial
    rollback. The previous "WHERE provider IN ('voyage', 'cohere') AND
    effective_from = ..." form was wider than the migration's actual
    insertions.
    """
    for provider, model, unit_type, _price, _denom in _SEED_ROWS:
        op.execute(
            _DELETE_PRICING_SQL.bindparams(
                provider=provider,
                model=model,
                unit_type=unit_type,
                effective_from=_SEED_EFFECTIVE_FROM,
            )
        )
