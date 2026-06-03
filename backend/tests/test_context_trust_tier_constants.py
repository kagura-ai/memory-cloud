"""Regression tests for #887: Context.trust_tier constant + CHECK invariants.

``trust_tier`` is the server-authoritative trust signal on Context:
``trusted`` (default) | ``external`` (connector-ingested). The CHECK is derived
from ``_ALL_CONTEXT_TRUST_TIERS`` so ``create_all()`` stays byte-identical to the
alembic head.
"""

from models.auth import (
    _ALL_CONTEXT_TRUST_TIERS,
    CONTEXT_TRUST_TIER_EXTERNAL,
    CONTEXT_TRUST_TIER_TRUSTED,
    Context,
)


def test_all_context_trust_tiers_tuple_matches_constants() -> None:
    assert _ALL_CONTEXT_TRUST_TIERS == (
        CONTEXT_TRUST_TIER_TRUSTED,
        CONTEXT_TRUST_TIER_EXTERNAL,
    )


def test_trust_tier_values() -> None:
    assert CONTEXT_TRUST_TIER_TRUSTED == "trusted"
    assert CONTEXT_TRUST_TIER_EXTERNAL == "external"


def test_valid_context_trust_tier_check_constraint_matches_migration_literal() -> None:
    expected = "trust_tier IN ('trusted', 'external')"
    check = next(
        c for c in Context.__table_args__ if getattr(c, "name", None) == "valid_context_trust_tier"
    )
    assert check.sqltext.text == expected


def test_trust_tier_column_defaults_to_trusted() -> None:
    """A context with no explicit trust_tier defaults to 'trusted' (server-side),
    so existing contexts and normal user contexts are unaffected."""
    col = Context.__table__.c.trust_tier
    assert col.nullable is False
    assert col.server_default.arg == "trusted"
