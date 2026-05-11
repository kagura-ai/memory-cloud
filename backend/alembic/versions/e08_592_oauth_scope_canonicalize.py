"""Canonicalize oauth_clients.scope to match advertised metadata (#592).

Issue #592 surfaced a 4-way drift between the OAuth metadata sources:

- ``GET /.well-known/oauth-authorization-server`` advertised
  ``{memory:read, memory:write, memory:admin, offline_access}``.
- ``GET /.well-known/oauth-protected-resource`` advertised
  ``{memory:read, memory:write, memory:delete, memory:admin}``
  (with a fictional ``memory:delete`` that nothing enforced, and missing
  ``offline_access``).
- ``GET /.well-known/openid-configuration`` advertised
  ``{openid, memory:read, memory:write, memory:admin, offline_access}``
  (with a fictional ``openid`` — the server does not issue ``id_token``).
- ``POST /api/v1/oauth/register`` (DCR) fell back to
  ``{memory:read, memory:write, offline_access}`` when the client did
  not specify a scope, silently omitting ``memory:admin``.

The Claude Code MCP SDK's scope-drift check (``Invalidated credentials
(scope: all)``) was tripping because the authorization URL combined the
union of advertised scopes while the DCR-issued client only held a
subset.

The application-level fix consolidates all four sources behind
``auth.mcp_scopes.ALL_ADVERTISED_SCOPES``. This migration normalises any
existing DCR-registered ``oauth_clients`` rows that still carry the
narrow pre-fix default so a refresh after deploy does not re-trip the
SDK's scope-drift cache eviction.

Scope of the data fix:

- Only rows whose ``scope`` exactly equals the pre-fix default are
  updated. Manually-managed or workspace-scoped clients (whose scope
  may legitimately be narrower than the canonical set) are NOT touched.
- The new canonical default is written as a single space-separated
  string matching ``auth.mcp_scopes.DCR_DEFAULT_SCOPE``.

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
_CANONICAL_AT_E08 = "memory:read memory:write memory:admin offline_access"


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
