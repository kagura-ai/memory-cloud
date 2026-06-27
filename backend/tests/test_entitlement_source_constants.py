"""Regression tests for #1095: Workspace.entitlement_source constant + CHECK.

``entitlement_source`` records WHO last set the entitlement so the external
billing reconciler reverts only what it owns: ``external_billing`` (billing-owned)
vs ``admin_grant`` (locally-owned, default). The CHECK is derived from
``ENTITLEMENT_SOURCES`` so ``create_all()`` stays byte-identical to the alembic
head (same single-source-of-truth pattern as ``_ALL_CONTEXT_TRUST_TIERS`` #887).
"""

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    ENTITLEMENT_SOURCES,
    Workspace,
)


def test_entitlement_sources_tuple_matches_constants() -> None:
    assert ENTITLEMENT_SOURCES == (
        ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
        ENTITLEMENT_SOURCE_ADMIN_GRANT,
    )


def test_entitlement_source_values() -> None:
    assert ENTITLEMENT_SOURCE_EXTERNAL_BILLING == "external_billing"
    assert ENTITLEMENT_SOURCE_ADMIN_GRANT == "admin_grant"


def test_valid_entitlement_source_check_constraint_matches_migration_literal() -> None:
    # Byte-identical to e46_1095_entitlement_source.py's CHECK predicate.
    expected = "entitlement_source IN ('external_billing', 'admin_grant')"
    check = next(
        c
        for c in Workspace.__table_args__
        if getattr(c, "name", None) == "valid_entitlement_source"
    )
    assert check.sqltext.text == expected


def test_entitlement_source_column_defaults_to_admin_grant() -> None:
    """The protective default: a never-billed workspace is locally-owned, so a
    reconcile pass leaves it untouched."""
    col = Workspace.__table__.c.entitlement_source
    assert col.nullable is False
    assert col.server_default.arg == ENTITLEMENT_SOURCE_ADMIN_GRANT
