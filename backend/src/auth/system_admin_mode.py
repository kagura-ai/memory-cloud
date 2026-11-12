"""Where the system-admin surface is served (Issue #2767).

``SYSTEM_ADMIN_MODE`` decides who may reach the routes behind
``auth.dependencies.require_admin`` on this process:

- ``session`` (default): a signed-in user whose ``users.role`` is ``admin``,
  exactly as before #2767.
- ``off``: nobody; the admin routes answer as if they did not exist.
- ``service``: only an operator console holding the service credential.

Every rule that reads the system role goes through the helpers here, so a
``role = 'admin'`` row grants nothing outside ``session`` mode. The module
reads settings only (no I/O) and must not import ``auth.roles`` (which imports
it).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, cast

from config.settings import get_settings

SystemAdminMode = Literal["session", "off", "service"]

# ``users.user_id`` values that no account may ever carry: an operator
# principal is ``operator:<address>``, so a real account with that id would
# make an operator's audit rows look like that account's. Known id formats
# today: numeric provider subjects, ``local:<login>`` (CLI), ``public:<id>``
# (public MCP) and ``email:<uuid4>`` (#1734).
OPERATOR_PRINCIPAL_PREFIX: Final = "operator:"
RESERVED_USER_ID_PREFIXES: Final[tuple[str, ...]] = (OPERATOR_PRINCIPAL_PREFIX,)

# The system role that the ``session`` surface admits. Kept as a string here:
# ``auth.roles.Role`` lives in a module that imports this one.
_SYSTEM_ADMIN_ROLE: Final = "admin"


def system_admin_mode() -> SystemAdminMode:
    """The mode this process serves (settings only, no I/O)."""
    return cast(SystemAdminMode, get_settings().system_admin_mode)


def session_admin_enabled() -> bool:
    """True when signed-in ``role = 'admin'`` users are system admins here."""
    return system_admin_mode() == "session"


def role_grants_system_admin(role: str | None) -> bool:
    """True when a ``users.role`` value carries system-admin power here.

    Only in ``session`` mode; in ``off`` and ``service`` an admin-role row is
    an ordinary account (no invite quota exemption, no promotion).
    """
    return session_admin_enabled() and role == _SYSTEM_ADMIN_ROLE


def is_effective_session_admin(user: Mapping[str, Any] | None) -> bool:
    """True for a session principal that is a system admin on this process."""
    if user is None:
        return False
    return role_grants_system_admin(user.get("role"))


def first_user_bootstrap_applies() -> bool:
    """Whether an empty users table lets the first sign-up through the gate.

    Kept in every mode (#2767, decision O-7): outside ``session`` the first
    account is admitted as an ordinary ``user`` (see
    ``auth.roles.new_account_role``), which is strictly less than ``session``
    grants the same visitor.
    """
    return True


def is_operator_principal(user: Mapping[str, Any] | None) -> bool:
    """True for the principal ``require_admin`` builds in ``service`` mode."""
    return user is not None and user.get("principal_kind") == "operator"
