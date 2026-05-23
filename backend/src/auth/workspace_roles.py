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
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "WorkspaceRole",
    "ContextRole",
    "WORKSPACE_ROLE_WEIGHTS",
    "CONTEXT_ROLE_WEIGHTS",
    "WORKSPACE_ROLE_CHECK_SQL",
    "CONTEXT_ROLE_CHECK_SQL",
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
