"""Verified, non-secret attribution for operation history, never authorization.

Only authentication adapters populate this context. No request headers, labels,
session cookies, token fragments or OAuth access-token IDs are retained here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

PrincipalKind = Literal[
    "session", "api_key", "oauth", "resource_token", "management_grant", "system", "unknown"
]


@dataclass(frozen=True)
class HistoryPrincipal:
    kind: PrincipalKind = "unknown"
    credential_id: str | None = None
    client_id: str | None = None


_principal: ContextVar[HistoryPrincipal | None] = ContextVar("history_principal", default=None)


def set_history_principal(
    kind: PrincipalKind = "unknown",
    *,
    credential_id: str | None = None,
    client_id: str | None = None,
) -> None:
    """Set attribution from a successfully verified database credential only."""
    _principal.set(HistoryPrincipal(kind, credential_id, client_id))


def get_history_principal() -> HistoryPrincipal:
    """Return known authentication evidence; absence means explicitly unknown."""
    return _principal.get() or HistoryPrincipal()


@contextmanager
def history_principal_scope(kind: PrincipalKind) -> Iterator[None]:
    """Attribute local maintenance explicitly, restoring the caller on exit."""
    token = _principal.set(HistoryPrincipal(kind))
    try:
        yield
    finally:
        _principal.reset(token)
