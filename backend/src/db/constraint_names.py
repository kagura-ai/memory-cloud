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

    Reads ``error.orig.diag.constraint_name`` (psycopg's structured
    diagnostic field). Robust against driver/locale changes, unlike
    substring matching on ``str(error)``. Returns ``None`` when the
    underlying driver did not surface a constraint name (e.g. non-Postgres
    backend, or a non-constraint integrity violation).
    """
    diag = getattr(getattr(error, "orig", None), "diag", None)
    if diag is None:
        return None
    return getattr(diag, "constraint_name", None)
