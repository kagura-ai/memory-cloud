"""``integrity_error_constraint_name`` on the error shapes the drivers raise (#2639).

SQLAlchemy's asyncpg dialect wraps the driver error in its emulated DBAPI
error: the constraint name sits one level further down than ``error.orig``.
Before #2639 the helper read ``error.orig`` only, so every mapping by name
(the 409s / 400s of a lost check-then-insert race) missed on asyncpg.
No database.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError

from db.constraint_names import integrity_error_constraint_name


class _DriverError(Exception):
    """Stands in for ``asyncpg.UniqueViolationError``: ``constraint_name`` set."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.constraint_name = constraint_name
        self.sqlstate = "23505"


def _wrapped(constraint_name: str | None) -> IntegrityError:
    """The asyncpg dialect's shape: the emulated error around the driver's."""
    emulated = AsyncAdapt_asyncpg_dbapi.UniqueViolationError(
        "duplicate key", _DriverError(constraint_name)
    )
    return IntegrityError("INSERT ...", {}, emulated)


def test_asyncpg_dialect_shape_reads_the_driver_error() -> None:
    assert integrity_error_constraint_name(_wrapped("uq_t_a")) == "uq_t_a"


def test_asyncpg_dialect_shape_without_a_name_is_none() -> None:
    assert integrity_error_constraint_name(_wrapped(None)) is None


def test_driver_error_set_directly_as_orig() -> None:
    error = IntegrityError("INSERT ...", {}, _DriverError("uq_t_b"))
    assert integrity_error_constraint_name(error) == "uq_t_b"


def test_psycopg_diag_shape() -> None:
    orig = Exception("duplicate key")
    orig.diag = SimpleNamespace(constraint_name="uq_t_c")  # type: ignore[attr-defined]
    error = IntegrityError("INSERT ...", {}, orig)
    assert integrity_error_constraint_name(error) == "uq_t_c"


def test_no_structured_diagnostics_is_none() -> None:
    error = IntegrityError("INSERT ...", {}, Exception("duplicate key"))
    assert integrity_error_constraint_name(error) is None
