"""Canonical MCP OAuth 2.0 scope definitions (single source of truth).

All ``scopes_supported`` advertisements (RFC 8414, RFC 9728, OIDC discovery)
derive from ``ALL_ADVERTISED_SCOPES``. The DCR (RFC 7591) fallback default
derives from ``DCR_DEFAULT_SCOPES``, which is intentionally narrower than
``ALL_ADVERTISED_SCOPES`` — see the policy notes below.

Policy notes:

- ``ALL_ADVERTISED_SCOPES`` is the UNION of every scope previously
  advertised by any of the four metadata sources at the time of #592.
  Keeping the union avoids a backward-incompatible removal in that
  hotfix — strict OIDC clients still see ``openid``, clients that
  whitelisted ``memory:delete`` still see it. Drift (every source
  advertising a DIFFERENT subset) was the actual bug; advertising the
  union from one shared constant is the fix.
- ``DCR_DEFAULT_SCOPES`` is the advertised set MINUS ``memory:admin``.
  ``memory:admin`` remains explicitly requestable via the DCR ``scope``
  parameter and is still advertised in ``scopes_supported``, but a
  client that omits ``scope`` no longer receives admin privileges by
  default. This is the narrowing-first ordering required by #608 (D1):
  every newly-issued DCR token is admin-less by default, so when a
  later sub-PR adds ``require_scope("memory:admin")`` to admin routes,
  no already-issued DCR client silently gains admin against the newly-
  protected routes. ``memory:delete`` stays in the default for now and
  is addressed separately in #608 (D4).

  SEP-835 (MCP authorization spec evolution) endorses this asymmetry:
  authorization servers MAY issue tokens with narrower scopes than
  requested, and clients SHOULD recover via 403 + ``WWW-Authenticate:
  insufficient_scope`` upgrade flows. A pre-SEP-835 strict-drift SDK may
  invalidate credentials when issued ⊂ requested; the rollback path if
  Claude Code MCP DCR breaks post-deploy is ``alembic downgrade
  e08_592_oauth_scope_canonicalize`` plus reverting this file's
  narrowing edit.
- ``openid`` is advertised on every metadata source so the discovery
  contract is consistent — but the server does NOT issue ``id_token``,
  so a strict OIDC client that proceeds past discovery will fail at the
  authorization step. The advertisement preserves discovery
  compatibility; the OIDC dishonesty is addressed separately in #608
  (D5), not part of this narrowing.
- ``offline_access`` (RFC 6749 §1.3.5 / §6) requests a refresh token.
"""

ALL_ADVERTISED_SCOPES: tuple[str, ...] = (
    "openid",
    "memory:read",
    "memory:write",
    "memory:admin",
    "memory:delete",
    "offline_access",
)

# DCR fallback default — advertised set minus ``memory:admin``. See module
# docstring (#608 D1) for the narrowing-first ordering rationale.
DCR_DEFAULT_SCOPES: tuple[str, ...] = tuple(s for s in ALL_ADVERTISED_SCOPES if s != "memory:admin")

DCR_DEFAULT_SCOPE: str = " ".join(DCR_DEFAULT_SCOPES)
