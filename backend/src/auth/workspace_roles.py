"""Workspace / Context role StrEnums (Issue #700).

Replaces stringly-typed role literals across the codebase. The system-level
``Role`` (admin / user / read_only) lives in ``auth.roles`` and is a separate
axis — this module is exclusively for tenancy roles inside a workspace.

The enum values are the existing DB strings; switching to StrEnum is a
type-safety upgrade, NOT a wire or storage change. ``StrEnum`` JSON-
serialises as its ``.value``, so API responses are byte-identical to the
pre-refactor shape.

The ``*_CHECK_SQL`` constants are derived from ``.value`` via f-string and
are byte-identical to the migration literals — re-used (Task 3 of #700) in
``models/auth.py`` ``CheckConstraint`` blocks so the enum is the single
source of truth for the DB constraint.

``Access`` (#1956) is the one "is this a read or a write" vocabulary the
permission chokepoints and the agent-binding evaluation share. It is a
separate axis from ``services.capacity_lock.CapacityPolicy``, which says
whether a call is subject to the capacity lock, not what it does.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "WorkspaceRole",
    "ContextRole",
    "WORKSPACE_ROLE_WEIGHTS",
    "CONTEXT_ROLE_WEIGHTS",
    "WORKSPACE_ROLE_CHECK_SQL",
    "CONTEXT_ROLE_CHECK_SQL",
    "Access",
    "ACCESS_READ",
    "ACCESS_WRITE",
    "access_for_workspace_role",
    "access_for_context_role",
]


class WorkspaceRole(StrEnum):
    """Workspace-level tenancy role.

    Ordering: OWNER > ADMIN > MEMBER > VIEWER (see WORKSPACE_ROLE_WEIGHTS).
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ContextRole(StrEnum):
    """Context-level tenancy role.

    Ordering: OWNER > EDITOR > VIEWER (see CONTEXT_ROLE_WEIGHTS).
    Distinct from WorkspaceRole — a context EDITOR is not a workspace
    ADMIN, and a workspace MEMBER may have no context role at all.
    """

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


WORKSPACE_ROLE_WEIGHTS: Mapping[WorkspaceRole, int] = MappingProxyType(
    {
        WorkspaceRole.OWNER: 4,
        WorkspaceRole.ADMIN: 3,
        WorkspaceRole.MEMBER: 2,
        WorkspaceRole.VIEWER: 1,
    }
)

CONTEXT_ROLE_WEIGHTS: Mapping[ContextRole, int] = MappingProxyType(
    {
        ContextRole.OWNER: 3,
        ContextRole.EDITOR: 2,
        ContextRole.VIEWER: 1,
    }
)


WORKSPACE_ROLE_CHECK_SQL: str = f"role IN ({', '.join(repr(r.value) for r in WorkspaceRole)})"

CONTEXT_ROLE_CHECK_SQL: str = f"role IN ({', '.join(repr(r.value) for r in ContextRole)})"


# What a call does to a context's content: ``read`` or ``write`` (#1956).
Access = Literal["read", "write"]
ACCESS_READ: Final[Access] = "read"
ACCESS_WRITE: Final[Access] = "write"


def access_for_workspace_role(role: WorkspaceRole | str) -> Access:
    """The access a required workspace role implies: VIEWER reads, any higher role writes.

    Args:
        role: The workspace role a check requires (enum or its string value).

    Returns:
        ``ACCESS_READ`` for viewer, else ``ACCESS_WRITE``.
    """
    return ACCESS_READ if role == WorkspaceRole.VIEWER else ACCESS_WRITE


def access_for_context_role(role: ContextRole | str) -> Access:
    """The access a required context role implies: VIEWER reads, any higher role writes.

    Args:
        role: The context role a check requires (enum or its string value).

    Returns:
        ``ACCESS_READ`` for viewer, else ``ACCESS_WRITE``.
    """
    return ACCESS_READ if role == ContextRole.VIEWER else ACCESS_WRITE
