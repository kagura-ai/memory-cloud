"""Narrow DCR default scope by stripping ``memory:admin`` (#608 D1).

#608 (D1) closes the "advertise-without-enforcement" gap left open by
#592 (v0.15.4). The hotfix kept the UNION of every previously-advertised
scope — including ``memory:admin`` — in ``DCR_DEFAULT_SCOPE`` to avoid a
backward-incompatible removal. That preserved compatibility but also
auto-granted ``memory:admin`` to every newly-registered DCR client. When
a future sub-PR adds ``require_scope("memory:admin")`` to admin routes,
every already-issued DCR token would silently gain admin against those
routes — a backward-compat privilege escalation. The narrowing-first
ordering closes this window: drop ``memory:admin`` from the DCR default
FIRST, then enforce on routes.

Scope of the data fix (R1 policy from gate1 review):

- Only DCR-registered clients are touched. The WHERE clause includes
  ``owner_id IS NULL`` — DCR clients have ``owner_id=NULL`` per
  ``d04_519_oauth_owner_nullable``; admin-managed clients have a
  non-null ``owner_id`` and are never touched by this migration even
  if their scope string happens to match the canonical.
- Among DCR clients, rows whose ``scope`` is exactly the post-#592
  canonical string get ``memory:admin`` stripped. This is the "got
  admin by default" cohort — clients that did not explicitly opt
  into admin and would have received it merely because the DCR
  fallback included it.
- Rows whose ``scope`` differs from the exact post-#592 canonical
  string are left untouched. This preserves any DCR client that
  registered with a custom scope set (subset, superset, or any
  ordering that doesn't match the canonical exactly).

Residual limitation (Copilot review on PR #615): the migration cannot
distinguish a DCR client that EXPLICITLY requested the full canonical
string (``scope="openid memory:read memory:write memory:admin
memory:delete offline_access"`` in this exact ordering) from a client
that received the same string via the fallback default. Such a client
would have ``memory:admin`` stripped despite having explicitly
requested it. In practice this collision is rare — it requires the
client to type out all six scopes in the server's canonical ordering,
which only matches clients that derived their scope list from the
server's discovery endpoint. Affected clients can recover by
re-registering with a distinguishing custom scope ordering, or by
operator-driven re-grant.

``memory:delete`` is intentionally NOT narrowed in this migration. The
distinct-vs-implied policy for ``memory:delete`` lands in #608 (D4) in
a separate migration; coarsening admin first, then handling delete as
its own least-privilege decision keeps the two concerns independent.

Downgrade applies the inverse: re-adds ``memory:admin`` on DCR clients
whose scope matches the post-narrow canonical. Same DCR-only filter
(``owner_id IS NULL``) and the same canonical-string residual
limitation apply in reverse — a DCR client that registered AFTER this
migration with the exact post-narrow canonical string would have
``memory:admin`` ADDED on downgrade (symmetric privilege escalation).
Operators running the downgrade should re-issue narrower scopes to
affected clients via the OAuth admin endpoints if this matters.

Rollback (operator runbook): if Claude Code MCP DCR breaks post-deploy
because a pre-SEP-835 SDK invalidates the narrowed token (the
``Invalidated credentials (scope: all)`` symptom that #592 originally
fixed), recovery is:

  1. ``alembic downgrade e08_592_oauth_scope_canonicalize`` — restores
     ``memory:admin`` on rows narrowed by this migration.
  2. Revert the ``DCR_DEFAULT_SCOPES`` narrowing in
     ``backend/src/auth/mcp_scopes.py`` (set ``DCR_DEFAULT_SCOPE = " ".join(
     ALL_ADVERTISED_SCOPES)`` again).
  3. Re-deploy. New DCR clients again receive the full union and the SDK
     drift check passes.

Standard Alembic ordering applies: deploy the application code BEFORE
running this migration (or atomically). A DCR registration that lands
between migration and deploy uses the old app's default and is not
touched by ``upgrade()``.

Revision ID: e09_608_dcr_default_narrow
Revises: e08_592_oauth_scope_canonicalize
"""

import sqlalchemy as sa

from alembic import op

revision = "e09_608_dcr_default_narrow"
down_revision = "e08_592_oauth_scope_canonicalize"
branch_labels = None
depends_on = None


# Hard-coded on purpose: an Alembic migration is a snapshot of intent at
# revision time. Importing ``auth.mcp_scopes`` would couple the migration
# to whatever the canonical set happens to be the day someone runs
# ``alembic upgrade head`` years later, which is the opposite of what
# migrations are for. The string below must match
# ``auth.mcp_scopes.DCR_DEFAULT_SCOPE`` as it was AT THIS REVISION — if
# the runtime constant changes again, write a new migration; do not edit
# this file.
_PRE_NARROW_CANONICAL = "openid memory:read memory:write memory:admin memory:delete offline_access"
_POST_NARROW_CANONICAL = "openid memory:read memory:write memory:delete offline_access"


def upgrade() -> None:
    # Strip memory:admin from DCR clients whose scope matches the
    # pre-narrow canonical exactly. ``owner_id IS NULL`` restricts to
    # DCR-registered clients (admin-managed clients have non-null
    # owner_id per d04_519_oauth_owner_nullable) — any custom-scoped
    # DCR client whose scope string differs from the canonical is also
    # untouched. See module docstring for the residual exact-match
    # canonical-ordering corner case.
    op.execute(
        sa.text(
            "UPDATE oauth_clients SET scope = :new_scope "
            "WHERE scope = :old_scope AND owner_id IS NULL"
        ).bindparams(new_scope=_POST_NARROW_CANONICAL, old_scope=_PRE_NARROW_CANONICAL)
    )


def downgrade() -> None:
    # Restore memory:admin on DCR clients whose scope matches the
    # post-narrow canonical. Symmetric ``owner_id IS NULL`` filter
    # excludes admin-managed clients. See module docstring for the
    # symmetric privilege-escalation note on the rare exact-match
    # corner case (DCR client registered post-upgrade with exact
    # post-narrow canonical string).
    op.execute(
        sa.text(
            "UPDATE oauth_clients SET scope = :old_scope "
            "WHERE scope = :new_scope AND owner_id IS NULL"
        ).bindparams(old_scope=_PRE_NARROW_CANONICAL, new_scope=_POST_NARROW_CANONICAL)
    )
