"""Canonical MCP OAuth 2.0 scope definitions (single source of truth).

All ``scopes_supported`` advertisements (RFC 8414, RFC 9728, OIDC discovery)
and the DCR (RFC 7591) fallback scope MUST derive from these constants.

Policy notes:

- ``memory:admin`` is advertised as a forward-compatibility commitment, NOT a
  permission gate. No route enforces it yet; advertising it lets DCR-issued
  clients hold the scope ahead of enforcement landing.
- ``memory:delete`` is intentionally NOT advertised — nothing enforces it.
  Reintroduce only with paired enforcement to avoid a fictional-scope drift.
- ``openid`` is intentionally NOT advertised on the OIDC discovery endpoint:
  this server does not issue ``id_token``. The discovery endpoint remains
  for MCP clients that probe it first.
- ``offline_access`` (RFC 6749 §1.3.5 / §6) requests a refresh token.
"""

ALL_ADVERTISED_SCOPES: list[str] = [
    "memory:read",
    "memory:write",
    "memory:admin",
    "offline_access",
]

DCR_DEFAULT_SCOPE: str = " ".join(ALL_ADVERTISED_SCOPES)
