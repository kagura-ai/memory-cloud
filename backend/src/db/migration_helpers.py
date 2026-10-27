"""Shared helpers for Alembic migrations (#2300).

Lock timeout for migration DDL
------------------------------
An ``ALTER TABLE`` needs ACCESS EXCLUSIVE. While it waits for that lock behind
a long transaction, every later query on the table queues behind the waiting
ALTER, so one slow reader can stall the whole API on a hot table
(``memories``, ``api_keys``, ``audit_logs``, ``memory_revisions``).
``alembic/env.py`` therefore starts every migration transaction with
``SET LOCAL lock_timeout``: a lock wait past it fails the migration with
SQLSTATE 55P03 (``lock_not_available``), the transaction rolls back, and the
operator runs the upgrade again (docs/deployment.md, "Migration lock
timeout").

- ``KAGURA_MIGRATION_LOCK_TIMEOUT`` sets the value (default ``5s``, a
  PostgreSQL duration such as ``500ms``, ``30s``, ``2min``; a bare number is
  milliseconds). ``0`` turns it off: no ``SET`` is sent at all.
- ``install_migration_lock_timeout`` applies it at every transaction begin of
  the migration connection: the first one and each one Alembic opens again
  after an ``autocommit_block()``.
- ``reapply_migration_lock_timeout`` (env.py's ``on_version_apply``) applies it
  again after each revision. An upgrade runs in one transaction, so without
  it a revision's own ``SET LOCAL lock_timeout`` / ``RESET lock_timeout``
  would carry over to the revisions after it.

A revision may still ``SET LOCAL lock_timeout`` to its own value; that wins
until the revision ends.

``SET LOCAL`` ends with its transaction, so it does not reach the statements
inside an ``autocommit_block()``. That is deliberate for ``CREATE INDEX
CONCURRENTLY``: its wait for older transactions does not block readers or
writers, and a timeout there would only leave an INVALID index behind. A
``VALIDATE CONSTRAINT`` in an autocommit block that wants a cap sets a
session-level one itself (``lock_timeout_statement(value, local=False)``,
then ``RESET lock_timeout``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection

MIGRATION_LOCK_TIMEOUT_ENV = "KAGURA_MIGRATION_LOCK_TIMEOUT"
DEFAULT_MIGRATION_LOCK_TIMEOUT = "5s"

# A PostgreSQL duration: an integer with an optional time unit (none means
# milliseconds). The value is interpolated into ``SET`` (``SET`` takes no bind
# parameters), so only this shape is accepted: ASCII digits, no sign, no space.
_DURATION = re.compile(r"([0-9]+)(ms|s|min|h|d)?")
_UNIT_MS = {None: 1, "ms": 1, "s": 1_000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}
# ``lock_timeout`` is an int of milliseconds; PostgreSQL refuses anything larger.
_MAX_MS = 2_147_483_647


def _duration_ms(value: str) -> int | None:
    """``value`` in milliseconds, or ``None`` if PostgreSQL would refuse it."""
    match = _DURATION.fullmatch(value)
    if match is None:
        return None
    ms = int(match.group(1)) * _UNIT_MS[match.group(2)]
    return ms if ms <= _MAX_MS else None


def migration_lock_timeout(environ: Mapping[str, str] | None = None) -> str | None:
    """The migration ``lock_timeout``, or ``None`` when it is turned off.

    Reads ``KAGURA_MIGRATION_LOCK_TIMEOUT`` (default ``5s``). ``0`` (in any
    unit) turns it off. Anything that is not an integer with an optional
    ``ms`` / ``s`` / ``min`` / ``h`` / ``d`` unit, or is past PostgreSQL's
    limit (2147483647 ms), raises ``ValueError`` before any schema change.
    """
    env = os.environ if environ is None else environ
    raw = env.get(MIGRATION_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_MIGRATION_LOCK_TIMEOUT
    ms = _duration_ms(raw)
    if ms is None:
        raise ValueError(
            f"{MIGRATION_LOCK_TIMEOUT_ENV}={raw!r} is not a PostgreSQL lock_timeout "
            "(an integer with an optional unit ms, s, min, h, d, at most 2147483647 ms; "
            "0 turns it off)"
        )
    return None if ms == 0 else raw


def lock_timeout_statement(value: str, *, local: bool = True) -> str:
    """``SET [LOCAL] lock_timeout = '<value>'`` for a validated duration.

    ``local=True`` lasts until the transaction ends; ``local=False`` lasts for
    the session (needed inside an ``autocommit_block()``; pair it with
    ``RESET lock_timeout``).
    """
    if _duration_ms(value) is None:
        raise ValueError(f"not a PostgreSQL lock_timeout: {value!r}")
    scope = "LOCAL " if local else ""
    return f"SET {scope}lock_timeout = '{value}'"


def _is_autocommit(connection: Connection) -> bool:
    """Whether ``connection`` runs in AUTOCOMMIT (per connection or engine-wide)."""
    level = connection.get_execution_options().get(
        "isolation_level", getattr(connection.dialect, "isolation_level", None)
    )
    return level == "AUTOCOMMIT"


def install_migration_lock_timeout(connection: Connection, value: str | None) -> None:
    """Start every transaction on ``connection`` with ``SET LOCAL lock_timeout``.

    ``value`` comes from ``migration_lock_timeout()``; ``None`` installs
    nothing. Call it once per ``Connection`` object (env.py opens one per
    run). Transactions begun in AUTOCOMMIT mode (``autocommit_block()``)
    are skipped: there is no transaction for ``SET LOCAL`` to last in. A
    transaction that is already open gets the setting now.
    """
    if value is None:
        return
    statement = lock_timeout_statement(value)

    def _on_begin(conn: Connection) -> None:
        if _is_autocommit(conn):
            return
        # Called from inside ``Connection.begin()``, before SQLAlchemy has
        # registered the transaction, so ``conn.execute`` would begin again.
        # The DBAPI cursor runs it in the transaction the driver opens.
        cursor = conn.connection.dbapi_connection.cursor()  # type: ignore[union-attr]
        try:
            cursor.execute(statement)
        finally:
            cursor.close()

    event.listen(connection, "begin", _on_begin)
    if connection.in_transaction() and not _is_autocommit(connection):
        connection.exec_driver_sql(statement)


def reapply_migration_lock_timeout(value: str | None):
    """An ``on_version_apply`` callback that applies ``value`` after each revision."""

    def _callback(*, ctx: Any, **_: Any) -> None:
        if value is None:
            return
        conn = ctx.connection
        if conn is None or not conn.in_transaction() or _is_autocommit(conn):
            return
        conn.exec_driver_sql(lock_timeout_statement(value))

    return _callback
