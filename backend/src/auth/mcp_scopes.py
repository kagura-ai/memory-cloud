"""Canonical MCP OAuth 2.0 scope definitions (single source of truth).

All ``scopes_supported`` advertisements (RFC 8414 authorization-server metadata,
RFC 9728 protected-resource metadata, OIDC discovery) and the DCR (RFC 7591)
fallback scope MUST derive from these constants. Issue #592 surfaced a 4-way
drift between three metadata endpoints and the DCR handler that broke the
Claude Code MCP OAuth flow; the drift-guard test in
``tests/api/test_oauth_metadata_drift.py`` pins this contract.

Authorization scopes are MCP-tier permission tokens (``memory:*``); they govern
what an authorized client may do against the protected resource (``/mcp/*``).
``offline_access`` is a meta-scope (RFC 6749 §1.3.5 / §6) that requests a
refresh token; it is advertised alongside the MCP scopes so refresh-capable
clients (Claude Code, ChatGPT) can request it without falling back to defaults.

Notes:
- ``memory:admin`` is advertised but not yet enforced in any route. The
  follow-up issue tracks adding ``requires_scope`` enforcement so that
  advertising this scope is not a fiction. Until then, treat its presence
  here as a forward-compatibility commitment, NOT a permission gate.
- ``memory:delete`` was previously advertised on the protected-resource
  metadata endpoint only; it is NOT enforced and has been removed to avoid
  another drift source. Reintroduce only with paired enforcement.
- ``openid`` was previously advertised on the OIDC discovery endpoint only;
  this server does not issue ``id_token`` (no OIDC subject claims path), so
  the OIDC advertisement was dishonest and has been removed. The discovery
  endpoint remains because MCP clients probe it first before
  ``oauth-authorization-server``.
"""

MCP_SCOPES: list[str] = [
    "memory:read",
    "memory:write",
    "memory:admin",
]
"""MCP permission scopes (granted to authorized clients)."""

MCP_REFRESH_SCOPE: str = "offline_access"
"""RFC 6749 §1.3.5 meta-scope requesting a refresh token."""

ALL_ADVERTISED_SCOPES: list[str] = [*MCP_SCOPES, MCP_REFRESH_SCOPE]
"""Every scope a discovery/metadata endpoint may advertise. All four metadata
sources (oauth-authorization-server, oauth-protected-resource,
openid-configuration, DCR response) must advertise exactly this set."""

DCR_DEFAULT_SCOPE: str = " ".join(ALL_ADVERTISED_SCOPES)
"""Space-separated default scope returned by RFC 7591 DCR when the client
does not request a specific scope. Matches ALL_ADVERTISED_SCOPES so a DCR
client immediately holds every advertised scope."""
