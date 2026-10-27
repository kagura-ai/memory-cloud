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
writers, and a timeout there would only leave an INVALID index behind.

Helpers for a revision (#2240)
------------------------------
New revisions call these instead of writing their own copies;
``tests/test_check_migration_helpers.py`` holds every revision after f17 to
that (the older ones keep their copies, allow-listed by revision id).

- ``validate_constraint(table, name)``: ``VALIDATE CONSTRAINT`` in a
  transaction of its own. The preceding ``NOT VALID`` add commits first (and
  releases its ACCESS EXCLUSIVE lock), and the VALIDATE commits right after
  its scan. Its transaction starts with the migration ``SET LOCAL
  lock_timeout`` like every other one, so no session state is set and it
  works through a transaction-pooling pgbouncer. Call it outside any
  autocommit block.
- ``session_lock_timeout(value)``: for statements that must run inside an
  ``autocommit_block()`` (a batched backfill that commits per batch): a
  session-level ``SET lock_timeout`` on entry and ``RESET lock_timeout`` on
  exit, the e99 / f14 / f15 form. A session-level setting needs a
  session-pinned connection (a direct connection or the Cloud SQL proxy, not
  a transaction-pooling pgbouncer).
- ``index_is_invalid(name)``: whether an interrupted ``CREATE INDEX
  CONCURRENTLY`` left ``name`` behind INVALID. A rerun's ``IF NOT EXISTS``
  would keep such an index, so the revision drops it (``DROP INDEX
  CONCURRENTLY IF EXISTS``) before it builds again. It reads the database,
  so a revision that calls it does not run offline (``--sql``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Connection

from alembic import op

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


# A table, constraint or index name interpolated into DDL (identifiers cannot
# be bound parameters): lowercase ASCII, unquoted, at most 63 bytes.
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# Only an index the search path finds: the one an unqualified ``DROP INDEX``
# that follows the probe would drop, not a same-named one in another schema.
_INVALID_INDEX_SQL = (
    "SELECT 1 FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
    "WHERE c.relname = :name AND pg_catalog.pg_table_is_visible(c.oid) "
    "AND i.indisvalid IS FALSE"
)


def _identifier(name: str) -> str:
    if _IDENTIFIER.fullmatch(name) is None:
        raise ValueError(f"not a plain lowercase SQL identifier: {name!r}")
    return name


@contextmanager
def session_lock_timeout(value: str | None) -> Iterator[None]:
    """Cap each lock wait of the statements inside an ``autocommit_block()``.

    ``SET lock_timeout`` (session-level, as ``SET LOCAL`` would end with each
    autocommitted statement) on entry, ``RESET lock_timeout`` on exit, also
    when the body raises. ``value`` is a duration (``migration_lock_timeout()``
    or a revision's own); ``None`` sets nothing. Enter it inside the block
    (``tests/test_check_migration_helpers.py`` checks that). It does not
    nest: ``RESET`` goes back to the server default, not to an outer value.
    """
    if value is None:
        yield
        return
    op.execute(sa.text(lock_timeout_statement(value, local=False)))
    try:
        yield
    finally:
        op.execute(sa.text("RESET lock_timeout"))


def _commit_point() -> None:
    """Commit the migration transaction here and begin the next one.

    An empty ``autocommit_block()``: alembic commits on entry and begins again
    on exit, and ``install_migration_lock_timeout`` starts that transaction
    with ``SET LOCAL lock_timeout``. Offline it emits ``COMMIT;`` /
    ``BEGIN;``.
    """
    with op.get_context().autocommit_block():
        pass


def validate_constraint(table: str, name: str) -> None:
    """``ALTER TABLE <table> VALIDATE CONSTRAINT <name>`` in a transaction of
    its own (#2073, #2079, #2240).

    A commit point before it releases the ``NOT VALID`` add's ACCESS
    EXCLUSIVE lock, so the scan holds only SHARE UPDATE EXCLUSIVE (and SHARE
    ROW EXCLUSIVE on a foreign key's referenced table). A commit point after
    it releases those as soon as the scan ends, not at the end of the
    upgrade. The VALIDATE's transaction begins with the migration ``SET LOCAL
    lock_timeout`` (``KAGURA_MIGRATION_LOCK_TIMEOUT``, through
    ``install_migration_lock_timeout``), so a lock wait past it fails the
    upgrade with SQLSTATE 55P03, to be run again. Everything before the call
    has committed, so it must be re-runnable (``DROP CONSTRAINT IF EXISTS
    ..., ADD ... NOT VALID``).

    Call it outside any ``autocommit_block()``: inside one there is no
    transaction to commit, and a live run raises ``RuntimeError``.
    """
    statement = f"ALTER TABLE {_identifier(table)} VALIDATE CONSTRAINT {_identifier(name)}"
    ctx = op.get_context()
    if not ctx.as_sql and ctx.connection is not None and _is_autocommit(ctx.connection):
        raise RuntimeError(
            "validate_constraint() commits around the VALIDATE itself; "
            "call it outside autocommit_block()"
        )
    _commit_point()
    op.execute(sa.text(statement))
    _commit_point()


def index_is_invalid(name: str) -> bool:
    """Whether index ``name`` exists in an INVALID state (the e62 probe).

    A ``CREATE INDEX CONCURRENTLY`` that fails mid-build leaves its index with
    ``indisvalid = false``. A rerun's ``IF NOT EXISTS`` would skip it without
    building it, so the revision drops the leftover first. Run the probe
    before the ``autocommit_block()`` that builds the index. It reads the
    database, so a revision that calls it does not run offline (``--sql``).
    """
    row = op.get_bind().execute(sa.text(_INVALID_INDEX_SQL), {"name": name}).first()
    return row is not None
