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

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

import config.settings as _config_settings
from utils.exceptions import (
    OperatorEmailInvalidError,
    SystemAdminCredentialError,
    SystemAdminNotConfiguredError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.routing import RouteContext

    from config.settings import Settings

SystemAdminMode = Literal["session", "off", "service"]

# ``users.user_id`` values that no account may ever carry: an operator
# principal is ``operator:<address>``, so a real account with that id would
# make an operator's audit rows look like that account's. Known id formats
# today: numeric provider subjects, ``local:<login>`` (CLI), ``public:<id>``
# (public MCP) and ``email:<uuid4>`` (#1734).
OPERATOR_PRINCIPAL_PREFIX: Final = "operator:"
RESERVED_USER_ID_PREFIXES: Final[tuple[str, ...]] = (OPERATOR_PRINCIPAL_PREFIX,)

# The operator console names the human behind each request in this header; it
# is read only after the service credential passed.
OPERATOR_EMAIL_HEADER: Final = "X-Operator-Email"
# Every actor column an operator id reaches is String(255).
OPERATOR_EMAIL_MAX_LEN: Final = 255 - len(OPERATOR_PRINCIPAL_PREFIX)
_OPERATOR_LOCAL_PART = re.compile(r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*")
_OPERATOR_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_BEARER_PREFIX: Final = "Bearer "

# The system role that the ``session`` surface admits. Kept as a string here:
# ``auth.roles.Role`` lives in a module that imports this one.
_SYSTEM_ADMIN_ROLE: Final = "admin"


def _settings() -> Settings:
    # Looked up at call time, so a test that swaps ``config.settings.get_settings``
    # (as the system-info tests do) is seen here too.
    return _config_settings.get_settings()


def system_admin_mode() -> SystemAdminMode:
    """The mode this process serves (settings only, no I/O)."""
    return _settings().system_admin_mode


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


def normalize_operator_email(values: Sequence[str]) -> str:
    """The operator's address from the ``X-Operator-Email`` header values.

    Exactly one value; surrounding blanks stripped, then ASCII only and
    lower-cased; at most ``OPERATOR_EMAIL_MAX_LEN`` characters so
    ``operator:<address>`` fits every actor column. The local part is limited
    to ``[a-z0-9._+-]`` (1 to 64, no leading, trailing or doubled dot): the
    value ends up in logs, audit ids and a rate-limit key. Raises
    ``OperatorEmailInvalidError`` (400) and never echoes the value.
    """
    if len(values) != 1:
        raise OperatorEmailInvalidError()
    raw = values[0].strip()
    # ASCII before lower-casing: str.lower() maps some non-ASCII letters (the
    # Kelvin sign) onto ASCII ones.
    if not raw.isascii():
        raise OperatorEmailInvalidError()
    address = raw.lower()
    if not 3 <= len(address) <= OPERATOR_EMAIL_MAX_LEN or address.count("@") != 1:
        raise OperatorEmailInvalidError()
    local, _, domain = address.partition("@")
    if (
        len(local) > 64
        or not _OPERATOR_LOCAL_PART.fullmatch(local)
        or not _OPERATOR_DOMAIN.fullmatch(domain)
    ):
        raise OperatorEmailInvalidError()
    return address


def build_operator_principal(address: str) -> dict[str, Any]:
    """The principal ``require_admin`` hands to admin handlers in ``service`` mode.

    ``user_id``, ``sub``, ``email`` and ``name`` all carry ``operator:<address>``
    so every stored actor string differs from a session admin's and from a bare
    address (an erasure matches ``audit_logs.user_email`` by value). The bare
    address is only in ``operator_email``. ``role`` is not ``admin``, so a
    forgotten role check fails closed; detection is by ``principal_kind``. No
    ``id`` key: there is no users row.
    """
    principal_id = f"{OPERATOR_PRINCIPAL_PREFIX}{address}"
    return {
        "user_id": principal_id,
        "sub": principal_id,
        "email": principal_id,
        "name": principal_id,
        "role": "operator",
        "principal_kind": "operator",
        "operator_email": address,
        "current_workspace_id": None,
    }


def operator_address(actor: str | None) -> str | None:
    """The bare, case-folded address behind an actor email string.

    ``operator:<address>`` and ``<address>`` give the same value, so a check
    that compares people (four-eyes) holds across ``session`` and ``service``.
    """
    if not actor:
        return None
    value = actor.strip()
    if value.startswith(OPERATOR_PRINCIPAL_PREFIX):
        value = value[len(OPERATOR_PRINCIPAL_PREFIX) :]
    return value.casefold() or None


def _credential_digest(value: str) -> bytes:
    # Digests of bytes: hmac.compare_digest raises on non-ASCII str, and a
    # header byte >= 0x80 arrives as one (latin-1). Same length both sides.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


def verify_service_credential(authorization: str | None) -> int:
    """Check the ``Authorization`` header against the service credential(s).

    Returns the matching slot: 0 for ``SYSTEM_ADMIN_SERVICE_TOKEN``, 1.. for a
    ``SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS`` entry. Every candidate is compared
    (no short-circuit), so the time does not tell which one matched or whether
    any did. Raises ``SystemAdminNotConfiguredError`` (503) when no credential
    is set and ``SystemAdminCredentialError`` (401) for a missing header,
    another scheme (``Basic``, a lower-case ``bearer``) or a wrong value.
    """
    settings = _settings()
    current = settings.system_admin_service_token
    if not current:
        raise SystemAdminNotConfiguredError()
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise SystemAdminCredentialError()
    presented = _credential_digest(authorization[len(_BEARER_PREFIX) :])
    matched: int | None = None
    for slot, candidate in enumerate((current, *settings.system_admin_service_previous_tokens)):
        if hmac.compare_digest(presented, _credential_digest(candidate)) and matched is None:
            matched = slot
    if matched is None:
        raise SystemAdminCredentialError()
    return matched


def route_requires_admin(route: Any) -> bool:
    """Whether a route's dependency tree contains ``require_admin``.

    ``route`` is an ``APIRoute`` or a ``fastapi.routing.RouteContext`` (whose
    ``dependant`` includes router-level dependencies). Walks the tree
    recursively, so ``require_admin_account`` (which depends on
    ``require_admin``) counts.
    """
    from auth.dependencies import require_admin

    def _depends_on_gate(dependant: Any) -> bool:
        return any(
            dep.call is require_admin or _depends_on_gate(dep) for dep in dependant.dependencies
        )

    dependant = getattr(route, "dependant", None)
    return dependant is not None and _depends_on_gate(dependant)


def routes_behind_require_admin(app: FastAPI) -> list[RouteContext]:
    """Every REST route of ``app`` behind ``require_admin``, with its full path.

    FastAPI registers included routers lazily, so the routes are read through
    ``fastapi.routing.iter_route_contexts`` (each context carries the mounted
    ``path``, ``methods`` and ``original_route``). Used to drop the admin
    operations from the OpenAPI document in ``off`` mode and by the tests that
    pin the admin surface.
    """
    from fastapi.routing import APIRoute, iter_route_contexts

    return [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and route_requires_admin(context)
    ]
