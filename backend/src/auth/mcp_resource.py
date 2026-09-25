"""The MCP resource this server publishes and how resource indicators match it.

``/.well-known/oauth-protected-resource`` publishes one ``resource`` (RFC 9728)
for the MCP endpoint: ``FRONTEND_URL`` plus ``MCP_BASE_PATH``. MCP clients send
that value, or the URL they were configured with, as the RFC 8707 ``resource``
parameter; a configured URL may be a path beneath the endpoint
(``/mcp/w/<workspace-id>``, ``/mcp/sse``) or carry a query (``?profile=core``).
Every such value names the same resource. Codes and tokens store the
published identifier, so one comparison decides an audience everywhere (#1686).
"""

from __future__ import annotations

import os
from urllib.parse import SplitResult, unquote, urlsplit

# Ports that a URL may leave out for its scheme.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def mcp_base_path() -> str:
    """The path the MCP endpoint is mounted at (``MCP_BASE_PATH``, default ``/mcp``)."""
    return os.getenv("MCP_BASE_PATH", "/mcp")


def mcp_resource_identifier() -> str:
    """The resource identifier of the MCP endpoint.

    This is the ``resource`` of ``/.well-known/oauth-protected-resource`` and
    the audience stored on authorization codes and tokens.

    Returns:
        ``FRONTEND_URL`` (without a trailing slash) followed by
        ``MCP_BASE_PATH``, e.g. ``https://memory.example.com/mcp``.
    """
    base_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base_url}{mcp_base_path()}"


def _origin(parts: SplitResult) -> tuple[str, str, int | None]:
    """Scheme, lower-case host and port of a URL, the scheme's default port dropped.

    Raises:
        ValueError: The port is not a valid number.
    """
    scheme = parts.scheme.lower()
    port = parts.port
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    return scheme, (parts.hostname or "").lower(), port


def is_same_mcp_resource(value: str) -> bool:
    """Whether a resource indicator names this server's MCP resource.

    It does when its origin is the published identifier's (same scheme, host
    compared case-insensitively, the scheme's default port optional, no user
    information) and its path is ``MCP_BASE_PATH`` or a path beneath it. Query
    and fragment are ignored. A path with a ``.`` or ``..`` segment does not
    match.

    Args:
        value: A resource indicator, e.g. the RFC 8707 ``resource`` parameter
            or the audience stored on a token.

    Returns:
        True when ``value`` names the MCP resource.
    """
    try:
        candidate = urlsplit(value)
        published = urlsplit(mcp_resource_identifier())
        if candidate.username is not None or candidate.password is not None:
            return False
        if not candidate.hostname or _origin(candidate) != _origin(published):
            return False
    except ValueError:  # an invalid port or a malformed IPv6 host
        return False

    path = candidate.path
    if any(segment in (".", "..") for segment in unquote(path).split("/")):
        return False
    base = published.path.rstrip("/")
    return path == base or path.startswith(f"{base}/")
