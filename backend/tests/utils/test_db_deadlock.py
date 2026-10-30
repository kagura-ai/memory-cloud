"""``is_deadlock`` recognises a Postgres deadlock victim (Issue #1985);
``is_lock_not_available`` a lock that was not granted at once (#2418)."""

from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from utils.db_helpers import PG_DEADLOCK_DETECTED, is_deadlock, is_lock_not_available


class _DriverError(Exception):
    def __init__(self, message: str, *, sqlstate: str | None = None, pgcode: str | None = None):
        super().__init__(message)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode


def test_asyncpg_adapter_sqlstate() -> None:
    orig = _DriverError("deadlock detected", sqlstate=PG_DEADLOCK_DETECTED)
    assert is_deadlock(DBAPIError("UPDATE", {}, orig))


def test_psycopg_pgcode() -> None:
    orig = _DriverError("deadlock detected", pgcode="40P01")
    assert is_deadlock(OperationalError("UPDATE", {}, orig))


def test_sqlstate_on_the_chained_driver_exception() -> None:
    orig = _DriverError("deadlock detected")
    orig.__cause__ = _DriverError("deadlock detected", sqlstate="40P01")
    assert is_deadlock(DBAPIError("UPDATE", {}, orig))


def test_other_errors_are_not_deadlocks() -> None:
    assert not is_deadlock(IntegrityError("INSERT", {}, _DriverError("dup", sqlstate="23505")))
    assert not is_deadlock(DBAPIError("UPDATE", {}, Exception("no sqlstate")))
    assert not is_deadlock(RuntimeError("40P01"))


def test_lock_not_available_from_either_driver() -> None:
    assert is_lock_not_available(DBAPIError("SELECT", {}, _DriverError("x", sqlstate="55P03")))
    assert is_lock_not_available(OperationalError("SELECT", {}, _DriverError("x", pgcode="55P03")))
    chained = _DriverError("could not obtain lock")
    chained.__cause__ = _DriverError("could not obtain lock", sqlstate="55P03")
    assert is_lock_not_available(DBAPIError("SELECT", {}, chained))


def test_a_deadlock_is_not_a_lock_not_available() -> None:
    deadlock = DBAPIError("UPDATE", {}, _DriverError("deadlock", sqlstate="40P01"))
    assert not is_lock_not_available(deadlock)
    assert not is_lock_not_available(RuntimeError("55P03"))
