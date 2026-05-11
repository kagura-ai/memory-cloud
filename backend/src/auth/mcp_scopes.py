"""Canonical MCP OAuth 2.0 scope definitions (single source of truth).

All ``scopes_supported`` advertisements (RFC 8414, RFC 9728, OIDC discovery)
and the DCR (RFC 7591) fallback scope MUST derive from these constants.

Policy notes:

- The canonical set is the UNION of every scope previously advertised by
  any of the four metadata sources at the time of #592. Keeping the union
  avoids a backward-incompatible removal in the hotfix — strict OIDC
  clients still get ``openid``, clients that whitelisted ``memory:delete``
  still see it. Drift (every source advertising a DIFFERENT subset) was
  the actual bug; advertising the union from one shared constant is the
  fix.
- ``memory:admin`` and ``memory:delete`` are advertised but not yet
  enforced on any route. Advertising them is a forward-compatibility
  commitment — DCR-issued clients hold the scope ahead of enforcement.
  #608 tracks paired enforcement; until that lands, treat these as
  unenforced fictional scopes (no permission gate, no implicit grant).
- ``openid`` is advertised on every metadata source so the discovery
  contract is consistent — but the server does NOT issue ``id_token``,
  so a strict OIDC client that proceeds past discovery will fail at the
  authorization step. The advertisement preserves discovery
  compatibility; the OIDC dishonesty is a separate cleanup, NOT part of
  the #592 drift fix.
- ``offline_access`` (RFC 6749 §1.3.5 / §6) requests a refresh token.
"""

ALL_ADVERTISED_SCOPES: list[str] = [
    "openid",
    "memory:read",
    "memory:write",
    "memory:admin",
    "memory:delete",
    "offline_access",
]

DCR_DEFAULT_SCOPE: str = " ".join(ALL_ADVERTISED_SCOPES)
