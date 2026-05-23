"""Test the WorkspaceRole + ContextRole StrEnum (#700)."""

import pytest

from auth.workspace_roles import (
    CONTEXT_ROLE_CHECK_SQL,
    CONTEXT_ROLE_WEIGHTS,
    WORKSPACE_ROLE_CHECK_SQL,
    WORKSPACE_ROLE_WEIGHTS,
    ContextRole,
    WorkspaceRole,
)


def test_workspace_role_values_match_db_literals() -> None:
    """Enum values must equal the existing DB CHECK literals byte-for-byte."""
    assert WorkspaceRole.OWNER.value == "owner"
    assert WorkspaceRole.ADMIN.value == "admin"
    assert WorkspaceRole.MEMBER.value == "member"
    assert WorkspaceRole.VIEWER.value == "viewer"


def test_context_role_values_match_db_literals() -> None:
    """Enum values must equal the existing DB CHECK literals byte-for-byte."""
    assert ContextRole.OWNER.value == "owner"
    assert ContextRole.EDITOR.value == "editor"
    assert ContextRole.VIEWER.value == "viewer"


def test_workspace_role_is_strenum() -> None:
    """StrEnum members compare equal to their string values (round-trip)."""
    assert WorkspaceRole.OWNER == "owner"
    assert "owner" == WorkspaceRole.OWNER
    assert WorkspaceRole("owner") is WorkspaceRole.OWNER


def test_context_role_is_strenum() -> None:
    """StrEnum members compare equal to their string values (round-trip)."""
    assert ContextRole.OWNER == "owner"
    assert "editor" == ContextRole.EDITOR
    assert ContextRole("viewer") is ContextRole.VIEWER


def test_workspace_role_weights_keyed_on_enum() -> None:
    """Weights dict is keyed on enum members for type-safe lookup."""
    assert WORKSPACE_ROLE_WEIGHTS[WorkspaceRole.OWNER] == 4
    assert WORKSPACE_ROLE_WEIGHTS[WorkspaceRole.ADMIN] == 3
    assert WORKSPACE_ROLE_WEIGHTS[WorkspaceRole.MEMBER] == 2
    assert WORKSPACE_ROLE_WEIGHTS[WorkspaceRole.VIEWER] == 1


def test_context_role_weights_keyed_on_enum() -> None:
    """Weights dict is keyed on enum members for type-safe lookup."""
    assert CONTEXT_ROLE_WEIGHTS[ContextRole.OWNER] == 3
    assert CONTEXT_ROLE_WEIGHTS[ContextRole.EDITOR] == 2
    assert CONTEXT_ROLE_WEIGHTS[ContextRole.VIEWER] == 1


def test_workspace_role_weights_immutable() -> None:
    """WORKSPACE_ROLE_WEIGHTS is a read-only view — mutation raises TypeError."""
    with pytest.raises(TypeError):
        WORKSPACE_ROLE_WEIGHTS[WorkspaceRole.OWNER] = 99  # type: ignore[index]


def test_workspace_role_check_sql_matches_existing_migration_literal() -> None:
    """f-string-derived CHECK SQL must equal the existing migration literal.

    The existing CheckConstraint in models/auth.py reads:
        role IN ('owner', 'admin', 'member', 'viewer')

    The refactor derives that string from the enum at runtime. Byte-identical
    parity is what lets ``Base.metadata.create_all()`` (test fixtures) stay
    drift-free from alembic head.
    """
    assert WORKSPACE_ROLE_CHECK_SQL == ("role IN ('owner', 'admin', 'member', 'viewer')")


def test_context_role_check_sql_matches_existing_migration_literal() -> None:
    assert CONTEXT_ROLE_CHECK_SQL == ("role IN ('owner', 'editor', 'viewer')")
