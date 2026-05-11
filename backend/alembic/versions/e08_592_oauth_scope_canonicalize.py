"""Canonicalize oauth_clients.scope to match advertised metadata (#592).

Issue #592 surfaced a 4-way drift between the OAuth metadata sources:

- ``GET /.well-known/oauth-authorization-server`` advertised
  ``{memory:read, memory:write, memory:admin, offline_access}``.
- ``GET /.well-known/oauth-protected-resource`` advertised
  ``{memory:read, memory:write, memory:delete, memory:admin}``
  (missing ``offline_access``).
- ``GET /.well-known/openid-configuration`` advertised
  ``{openid, memory:read, memory:write, memory:admin, offline_access}``.
- ``POST /api/v1/oauth/register`` (DCR) fell back to
  ``{memory:read, memory:write, offline_access}`` when the client did
  not specify a scope, silently omitting ``memory:admin`` and other
  scopes advertised by the metadata.

The Claude Code MCP SDK's scope-drift check (``Invalidated credentials
(scope: all)``) was tripping because the authorization URL combined the
union of advertised scopes while the DCR-issued client only held a
subset.

The application-level fix consolidates all four sources behind
``auth.mcp_scopes.ALL_ADVERTISED_SCOPES`` — which is the UNION of every
scope previously advertised by any source, so no external client sees a
removed scope. This migration normalises any existing DCR-registered
``oauth_clients`` rows that still carry the narrow pre-fix default so a
refresh after deploy does not re-trip the SDK's scope-drift cache
eviction.

Scope of the data fix:

- Rows whose ``scope`` differs from the exact pre-fix default string are
  left untouched. This preserves any explicitly-narrower client scope
  (e.g. ``memory:read`` only).
- Rows with the exact pre-fix default string are widened to canonical
  regardless of how they were registered. This intentionally includes
  admin-managed clients that were created without an explicit
  ``scope=`` override (the admin Pydantic schema also defaulted to the
  pre-fix narrow string before #592). Widening them is benign because
  the added scopes (``memory:admin``, ``memory:delete``, ``openid``) are
  not enforced on any route yet — they are advertised solely to keep the
  four metadata sources self-consistent. #608 tracks paired enforcement.
- The canonical default is a single space-separated string matching
  ``auth.mcp_scopes.DCR_DEFAULT_SCOPE``.

Downgrade restores the narrow scope on rows we widened. We can identify
them precisely because the canonical and pre-fix strings differ.

Revision ID: e08_592_oauth_scope_canonicalize
Revises: e07_556_sha256_lowercase_index
"""

import sqlalchemy as sa

from alembic import op

revision = "e08_592_oauth_scope_canonicalize"
down_revision = "e07_556_sha256_lowercase_index"
branch_labels = None
depends_on = None


# Hard-coded here on purpose: an Alembic migration is a snapshot of intent at
# revision time. Importing ``auth.mcp_scopes`` would couple the migration to
# whatever the canonical set happens to be the day someone runs ``alembic
# upgrade head`` years later, which is the opposite of what migrations are for.
_PRE_FIX_DEFAULT = "memory:read memory:write offline_access"
_CANONICAL_AT_E08 = "openid memory:read memory:write memory:admin memory:delete offline_access"


def upgrade() -> None:
    # Widen DCR-issued clients that still hold the pre-#592 narrow default.
    # Matching on the exact string keeps custom scopes (e.g. workspace-scoped
    # admin clients with only ``memory:read``) untouched.
    op.execute(
        sa.text("UPDATE oauth_clients SET scope = :new_scope WHERE scope = :old_scope").bindparams(
            new_scope=_CANONICAL_AT_E08, old_scope=_PRE_FIX_DEFAULT
        )
    )


def downgrade() -> None:
    # Restore the narrow scope on the rows we widened. Anything we did not
    # touch in upgrade() stays put.
    op.execute(
        sa.text("UPDATE oauth_clients SET scope = :old_scope WHERE scope = :new_scope").bindparams(
            old_scope=_PRE_FIX_DEFAULT, new_scope=_CANONICAL_AT_E08
        )
    )
