"""The OAuth scope rule shared by the authorization and device endpoints (#1686).

``granted_scope`` decides what an authorization request is granted, from the
requested scope and the client's registered scope (``client_registered_scope``).
``registration_scope`` decides what Dynamic Client Registration stores.
``OAuth2Client.get_allowed_scope``, which Authlib calls while it validates an
authorization request, delegates here too, so every path grants the same scope.

This module depends only on the scope constants, so the client model can
import it.
"""

from __future__ import annotations

from typing import Any

from auth.mcp_scopes import ALL_ADVERTISED_SCOPES, DCR_DEFAULT_SCOPES


def _names_memory_scope(scopes: list[str]) -> bool:
    return any(scope.startswith("memory:") for scope in scopes)


def granted_scope(requested: str | None, registered: str | None) -> str:
    """Scope an authorization request is granted (RFC 6749 §3.3).

    The requested scopes that the client registered and that this server
    defines (``ALL_ADVERTISED_SCOPES``); other requested scopes are dropped,
    which §3.3 allows, and the token response carries the granted ``scope``.
    When that leaves no ``memory:*`` scope (the request had none the client
    may have, e.g. only ``openid`` / ``offline_access`` or scopes this server
    does not define, or no ``scope`` at all), the client's registered scope
    that this server defines is granted instead.

    Args:
        requested: The ``scope`` parameter of the request, if any.
        registered: The client's registered scope.

    Returns:
        The granted scopes, space-separated, in request (or registration)
        order without duplicates. Empty only when the registered scope has
        no ``memory:*`` scope this server defines.
    """
    advertised = set(ALL_ADVERTISED_SCOPES)
    registration = [
        scope for scope in dict.fromkeys((registered or "").split()) if scope in advertised
    ]
    if not _names_memory_scope(registration):
        return ""
    allowed = set(registration)
    granted = [scope for scope in dict.fromkeys((requested or "").split()) if scope in allowed]
    if _names_memory_scope(granted):
        return " ".join(granted)
    return " ".join(registration)


def registration_scope(requested: str | None) -> str:
    """Scope stored for a Dynamic Client Registration (RFC 7591).

    The requested scopes that this server defines. A request without
    ``scope``, or whose scopes this server defines include no ``memory:*``
    scope (e.g. ``claudeai`` or ``openid offline_access``), is registered
    with ``DCR_DEFAULT_SCOPE``, keeping any ``openid`` / ``offline_access`` it
    asked for.

    Args:
        requested: The ``scope`` of the registration request, if any.

    Returns:
        The scope to store, space-separated, without duplicates.
    """
    advertised = set(ALL_ADVERTISED_SCOPES)
    kept = [scope for scope in dict.fromkeys((requested or "").split()) if scope in advertised]
    if _names_memory_scope(kept):
        return " ".join(kept)
    return " ".join(dict.fromkeys([*DCR_DEFAULT_SCOPES, *kept]))


def client_registered_scope(client: Any) -> str:
    """The registered scope the scope rule works from.

    A DCR client (``owner_id`` is ``None``) whose stored scope has no
    ``memory:*`` scope this server defines is treated as registered with
    :func:`registration_scope` of that scope, the scope ``/register`` stores
    for such a request. An admin-managed client's scope is used as stored.

    Args:
        client: The OAuth client (``scope`` and ``owner_id`` are read).

    Returns:
        Its registered scope, space-separated.
    """
    stored = client.scope or ""
    if getattr(client, "owner_id", None) is not None:
        return stored
    advertised = set(ALL_ADVERTISED_SCOPES)
    if _names_memory_scope([scope for scope in stored.split() if scope in advertised]):
        return stored
    return registration_scope(stored)
