"""#2904: which indexer event failures are a busy lock (retry) and which are not.

The DB behaviour (a second session holding the lock) is in
``tests/integration/test_sync_index_lock_busy_db.py``.
"""

from sqlalchemy.exc import DBAPIError

from services.sync_feed_provenance import sync_lock_busy
from utils.exceptions import ConflictError, QdrantError, SyncLockBusyError


class _DriverError(Exception):
    def __init__(self, message: str, *, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _database_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT", {}, _DriverError("driver", sqlstate=sqlstate))


def test_busy_error_keeps_the_conflict_status_code_and_message() -> None:
    busy = SyncLockBusyError()
    plain = ConflictError("Synchronization state is changing; retry")
    assert isinstance(busy, ConflictError)
    assert (busy.status_code, busy.error_code, busy.message, busy.details) == (
        plain.status_code,
        plain.error_code,
        plain.message,
        plain.details,
    )
    assert SyncLockBusyError("Feed management is changing; retry").status_code == 409


def test_busy_error_and_lock_not_available_are_retries() -> None:
    assert sync_lock_busy(SyncLockBusyError())
    assert sync_lock_busy(_database_error("55P03"))


def test_lock_not_available_under_a_wrapper_is_a_retry() -> None:
    wrapped = QdrantError("Failed to delete document")
    wrapped.__cause__ = _database_error("55P03")
    assert sync_lock_busy(wrapped)


def test_deterministic_conflicts_and_other_database_errors_are_not_retries() -> None:
    assert not sync_lock_busy(ConflictError("Synchronization document was independently edited"))
    assert not sync_lock_busy(_database_error("23505"))
    assert not sync_lock_busy(_database_error("40P01"))
    wrapped = QdrantError("Failed to delete document")
    wrapped.__cause__ = ConflictError("Synchronization memory ownership changed")
    assert not sync_lock_busy(wrapped)
    assert not sync_lock_busy(RuntimeError("55P03"))


def test_a_cause_cycle_ends() -> None:
    first, second = ConflictError("a"), ConflictError("b")
    first.__cause__, second.__cause__ = second, first
    assert not sync_lock_busy(first)
