"""Regression tests for #1274: Agent Registry constant + CHECK invariants.

The ``agents`` CHECK constraints are derived from ordered Python tuples
(``_ALL_AGENT_STATUSES`` / ``_ALL_AGENT_ENFORCEMENT_MODES``) so
``Base.metadata.create_all()`` (tests, fresh dev DBs) produces CHECK strings
byte-identical to the alembic migration literal (``e63_1274_agents``) —
the ``valid_delivery_mode`` drift-pin pattern (#886).

Adding a value requires THREE coordinated edits (caught here if any are
missed): (1) add the named constant, (2) APPEND it to the tuple (never
reorder), (3) update the expected literal below — plus an alembic migration
that ALTERs the prod CHECK.
"""

from models.agent import (
    _ALL_AGENT_ENFORCEMENT_MODES,
    _ALL_AGENT_STATUSES,
    AGENT_ENFORCEMENT_ENFORCE,
    AGENT_ENFORCEMENT_SHADOW,
    AGENT_STATUS_ACTIVE,
    AGENT_STATUS_RETIRED,
    AGENT_STATUS_SUSPENDED,
    Agent,
)


def test_all_agent_statuses_tuple_matches_constants() -> None:
    """Registration order is fixed (active, suspended, retired)."""
    assert _ALL_AGENT_STATUSES == (
        AGENT_STATUS_ACTIVE,
        AGENT_STATUS_SUSPENDED,
        AGENT_STATUS_RETIRED,
    )


def test_all_agent_enforcement_modes_tuple_matches_constants() -> None:
    """Registration order is fixed (shadow, enforce) — matches the DDL literal."""
    assert _ALL_AGENT_ENFORCEMENT_MODES == (
        AGENT_ENFORCEMENT_SHADOW,
        AGENT_ENFORCEMENT_ENFORCE,
    )


def test_agent_constant_values() -> None:
    """The design-fixed value set (docs/design/agent-registry-and-bindings.md)."""
    assert AGENT_STATUS_ACTIVE == "active"
    assert AGENT_STATUS_SUSPENDED == "suspended"
    assert AGENT_STATUS_RETIRED == "retired"
    assert AGENT_ENFORCEMENT_SHADOW == "shadow"
    assert AGENT_ENFORCEMENT_ENFORCE == "enforce"


def test_valid_agent_status_check_matches_migration_literal() -> None:
    """``valid_agent_status`` CHECK text is byte-identical to e63_1274_agents."""
    expected = "status IN ('active', 'suspended', 'retired')"
    check = next(
        c for c in Agent.__table_args__ if getattr(c, "name", None) == "valid_agent_status"
    )
    assert check.sqltext.text == expected


def test_valid_agent_enforcement_check_matches_migration_literal() -> None:
    """``valid_agent_enforcement`` CHECK text is byte-identical to e63_1274_agents."""
    expected = "enforcement_mode IN ('shadow', 'enforce')"
    check = next(
        c for c in Agent.__table_args__ if getattr(c, "name", None) == "valid_agent_enforcement"
    )
    assert check.sqltext.text == expected


def test_agents_is_classified_operational() -> None:
    """#1274 two-sided closure: the creating PR classifies the table."""
    from models.data_boundary import OPERATIONAL_TABLES

    assert "agents" in OPERATIONAL_TABLES


def test_all_binding_write_policies_tuple_matches_constants() -> None:
    """Registration order is fixed (deny, direct); 'staged' is reserved for P1."""
    from models.agent import (
        _ALL_BINDING_WRITE_POLICIES,
        BINDING_WRITE_POLICY_DENY,
        BINDING_WRITE_POLICY_DIRECT,
    )

    assert _ALL_BINDING_WRITE_POLICIES == (
        BINDING_WRITE_POLICY_DENY,
        BINDING_WRITE_POLICY_DIRECT,
    )
    assert BINDING_WRITE_POLICY_DENY == "deny"
    assert BINDING_WRITE_POLICY_DIRECT == "direct"


def test_valid_binding_write_policy_check_matches_migration_literal() -> None:
    """``valid_binding_write_policy`` CHECK text is byte-identical to e64."""
    from models.agent import AgentContextBinding

    expected = "write_policy IN ('deny', 'direct')"
    check = next(
        c
        for c in AgentContextBinding.__table_args__
        if getattr(c, "name", None) == "valid_binding_write_policy"
    )
    assert check.sqltext.text == expected


def test_agent_context_bindings_is_classified_operational() -> None:
    """#1275 two-sided closure: the creating PR classifies the table."""
    from models.data_boundary import OPERATIONAL_TABLES

    assert "agent_context_bindings" in OPERATIONAL_TABLES


def test_api_keys_agent_exclusion_check_matches_migration_literal() -> None:
    """``ck_api_keys_agent_public_exclusion`` matches the e65 literal."""
    from models.auth import APIKey

    expected = "agent_id IS NULL OR bound_context_id IS NULL"
    check = next(
        c
        for c in APIKey.__table_args__
        if getattr(c, "name", None) == "ck_api_keys_agent_public_exclusion"
    )
    assert check.sqltext.text == expected


def test_api_keys_agent_requires_workspace_check_matches_migration_literal() -> None:
    """``ck_api_keys_agent_requires_workspace`` matches the e67 literal (#1281)."""
    from models.auth import APIKey

    expected = "agent_id IS NULL OR workspace_id IS NOT NULL"
    check = next(
        c
        for c in APIKey.__table_args__
        if getattr(c, "name", None) == "ck_api_keys_agent_requires_workspace"
    )
    assert check.sqltext.text == expected
