"""Link connector write keys by foreign key: api_keys.connector_id (#1952).

A connector's KMC write key was tied to its connector only by its name,
``connector:{id}`` (#1944): ``verify_key`` parsed the name to pause the key
with the connector's seats, ``delete_connector`` revoked it by name. A
connector removed through the ``resources`` / ``workspaces`` cascades left its
key alive as a plain workspace-wide write key. The link is now a column:

- ``connector_id`` UUID NULL, ``fk_api_keys_connector_id`` →
  ``workspace_connectors(id)`` ``ON DELETE CASCADE`` (not SET NULL: a key
  outliving its connector would be an unowned workspace write key — the
  ``agent_id`` argument of e65);
- backfill: every key of the same workspace whose name is exactly
  ``connector:`` + the connector id in the canonical lowercase-hyphenated
  spelling (what ``connector_key_name`` writes), revoked rows included. The
  suffix is compared as text, never cast, so a junk suffix cannot raise.
  Look-alikes naming another workspace's connector, a missing id, or a
  non-canonical spelling stay NULL (ordinary keys, as they were). Rows the
  CHECKs below would refuse (agent-bound, a default context) stay NULL too;
- three CHECKs keep a connector key a plain workspace key:
  ``ck_api_keys_connector_requires_workspace``, ``_not_agent``,
  ``_not_default`` (not public-bound follows from requires_workspace plus
  ``ck_api_keys_binding_workspace_exclusion``);
- ``idx_api_keys_connector``, partial like ``idx_api_keys_agent``.

``api_keys`` is read on every authenticated request, so this follows the
e65 hot-table pattern: the column and the NOT VALID CHECKs are metadata-only
under the ALTER's ACCESS EXCLUSIVE lock; the backfill, VALIDATE (SHARE UPDATE
EXCLUSIVE, #2073) and ``CREATE INDEX CONCURRENTLY`` run inside
``autocommit_block()``, after that lock is released. Everything before the
block is re-runnable (``IF NOT EXISTS``, ``DROP CONSTRAINT IF EXISTS, ADD``) and the
backfill only touches ``connector_id IS NULL`` rows, so a partial failure can
be re-run.

Downgrade drops the index, the CHECKs and the column (with its FK). Key
names are untouched, so the name-based code before #1952 keeps working.

Revision ID: f13_1952_api_keys_connector_id
Revises: f12_2244_rev_writer_erasure
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f13_1952_api_keys_connector_id"
down_revision: str | None = "f12_2244_rev_writer_erasure"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "idx_api_keys_connector"
_INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
    "ON api_keys (connector_id) WHERE connector_id IS NOT NULL"
)
_CK_REQUIRES_WORKSPACE = "ck_api_keys_connector_requires_workspace"
_CK_NOT_AGENT = "ck_api_keys_connector_not_agent"
_CK_NOT_DEFAULT = "ck_api_keys_connector_not_default"
_CHECKS = (_CK_REQUIRES_WORKSPACE, _CK_NOT_AGENT, _CK_NOT_DEFAULT)

# Same-workspace name match, text comparison only (see the module docstring).
# ``uuid::text`` is the canonical lowercase-hyphenated spelling, exactly what
# ``connector_key_name`` writes.
_BACKFILL = (
    "UPDATE api_keys AS k "
    "SET connector_id = c.id "
    "FROM workspace_connectors AS c "
    "WHERE k.connector_id IS NULL "
    "AND k.workspace_id IS NOT NULL "
    "AND k.agent_id IS NULL "
    "AND k.bound_context_id IS NULL "
    "AND k.default_context_id IS NULL "
    "AND k.name LIKE 'connector:%' "
    "AND substr(k.name, length('connector:') + 1) = c.id::text "
    "AND c.workspace_id = k.workspace_id"
)


def _index_is_invalid(name: str) -> bool:
    """True if ``name`` exists in ``pg_index`` in an INVALID state (e62 guard).

    A mid-build failure of ``CREATE INDEX CONCURRENTLY`` leaves the index row
    with ``indisvalid = false``; a retry's ``IF NOT EXISTS`` would skip it
    without rebuilding, so the leftover must be dropped first.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 "
            "FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND i.indisvalid IS FALSE"
        ),
        {"name": name},
    ).first()
    return row is not None


def upgrade() -> None:
    """Add the column, its CHECKs and index, and link the existing keys."""
    # Step 1: nullable FK column — metadata only, rerun-safe. The FK is named
    # after the ORM naming convention so the create_all/alembic drift gate
    # sees one identical constraint.
    op.execute(
        sa.text(
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS connector_id UUID NULL "
            "CONSTRAINT fk_api_keys_connector_id "
            "REFERENCES workspace_connectors(id) ON DELETE CASCADE"
        )
    )

    # Step 2: the CHECKs, NOT VALID (no scan). ``DROP IF EXISTS, ADD`` in one
    # ALTER keeps the step re-runnable (f00_2019 form). Literals are
    # byte-identical to models/auth.py (tests/test_schema_drift.py).
    op.execute(
        sa.text(
            "ALTER TABLE api_keys "
            f"DROP CONSTRAINT IF EXISTS {_CK_REQUIRES_WORKSPACE}, "
            f"ADD CONSTRAINT {_CK_REQUIRES_WORKSPACE} "
            "CHECK (connector_id IS NULL OR workspace_id IS NOT NULL) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE api_keys "
            f"DROP CONSTRAINT IF EXISTS {_CK_NOT_AGENT}, "
            f"ADD CONSTRAINT {_CK_NOT_AGENT} "
            "CHECK (connector_id IS NULL OR agent_id IS NULL) NOT VALID"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE api_keys "
            f"DROP CONSTRAINT IF EXISTS {_CK_NOT_DEFAULT}, "
            f"ADD CONSTRAINT {_CK_NOT_DEFAULT} "
            "CHECK (connector_id IS NULL OR default_context_id IS NULL) NOT VALID"
        )
    )

    # Step 3: after the ALTER commits — the backfill (row locks only; the NOT
    # VALID CHECKs already apply to it and its WHERE keeps clear of them),
    # VALIDATE (SHARE UPDATE EXCLUSIVE; a no-op on re-run) and the partial
    # index built CONCURRENTLY.
    invalid = _index_is_invalid(_INDEX_NAME)
    with op.get_context().autocommit_block():
        op.execute(sa.text(_BACKFILL))
        for name in _CHECKS:
            op.execute(sa.text(f"ALTER TABLE api_keys VALIDATE CONSTRAINT {name}"))
        if invalid:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
        op.execute(sa.text(_INDEX_DDL))


def downgrade() -> None:
    """Drop the index, the CHECKs and the column (its FK goes with it)."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    for name in reversed(_CHECKS):
        op.execute(sa.text(f"ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS {name}"))
    op.execute(sa.text("ALTER TABLE api_keys DROP COLUMN IF EXISTS connector_id"))
