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
