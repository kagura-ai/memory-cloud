"""Single source of truth for DB constraint and index names.

Shared by Alembic migrations and IntegrityError handlers in the application
layer. Importing the same name from both sides prevents the code/schema
drift that motivated issue #318: a constraint name typo was caught by
substring matching ``str(IntegrityError)``, so a rename in the migration
silently disabled the 409 path.

Conventions:
    - Add a new constant here whenever the application needs to recognize
      a constraint or partial-unique index by name in an IntegrityError.
    - Never delete or rename an entry without also writing a follow-up
      migration that renames the underlying DB object.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

RESOURCE_EVENTS_UPSERT_UNIQUE = "ux_resource_events_upsert_version"

# PostgreSQL default ``<table>_<column>_key`` for the unnamed
# ``sa.UniqueConstraint("idempotency_key")`` in baseline 157247e0df86.
RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE = "resource_events_idempotency_key_key"

# Issue #385: partial unique index on
# (workspace_id, context_id, provider) WHERE enabled=true
# with NULLS NOT DISTINCT (PG 15+). Created by migration a99 + mirrored in
# models.auth.ExternalAPIKey.__table_args__. The three-column key supports the
# service-layer's context > workspace priority contract: a context-scoped
# enabled key and a workspace-scoped (context_id IS NULL) fallback for the same
# provider coexist, while NULLS NOT DISTINCT blocks two workspace-scoped rows
# for the same provider.
#
# Used by external_keys routes (create / toggle) to map IntegrityError → 409
# only when this specific constraint fires; other IntegrityErrors stay as 500.
EXTERNAL_API_KEYS_WORKSPACE_PROVIDER_ENABLED_UNIQUE = (
    "uq_external_api_keys_workspace_provider_enabled"
)

# Issue #385: full unique index on (workspace_id, key_name). Guarantees that
# scalar_one_or_none() lookups in update/toggle/delete handlers cannot raise
# MultipleResultsFound on legacy data (pre-#381, multiple users could each create
# keys with the same key_name in one workspace). Created by migration a99.
EXTERNAL_API_KEYS_WORKSPACE_KEY_NAME_UNIQUE = "uq_external_api_keys_workspace_key_name"

# Issue #1274: unique index on agents (workspace_id, name). Created by
# migration e63 + mirrored in models.agent.Agent.__table_args__. Used by
# AgentRegistryService (create / rename) to close the duplicate-check→insert
# TOCTOU race: concurrent registration of the same name loses the race at
# flush, and the IntegrityError is mapped to the same ConflictError (409)
# the pre-check produces; other IntegrityErrors propagate unchanged.
AGENTS_WORKSPACE_NAME_UNIQUE = "uq_agents_workspace_name"


def integrity_error_constraint_name(error: IntegrityError) -> str | None:
    """Return the PostgreSQL constraint name for ``error``, or ``None``.

    Handles both driver shapes this project runs into:

        - asyncpg (production): ``error.orig`` is an ``asyncpg.UniqueViolationError``
          or similar with ``constraint_name`` as a direct attribute.
        - psycopg / psycopg2 (sync integration tests, Alembic): ``error.orig``
          exposes a ``diag`` namespace with ``constraint_name`` inside.

    Checked in that order because the async path is the hot path. Returns
    ``None`` when neither shape is present (non-Postgres backend, driver
    without structured diagnostics, or non-constraint integrity violation).
    """
    orig = getattr(error, "orig", None)
    if orig is None:
        return None
    direct = getattr(orig, "constraint_name", None)
    if direct:
        return direct
    diag = getattr(orig, "diag", None)
    if diag is None:
        return None
    return getattr(diag, "constraint_name", None)
