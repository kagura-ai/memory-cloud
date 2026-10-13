"""#1961: the reducible-storage figure is one statement, used by both gates."""

from __future__ import annotations

import inspect
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from services import capacity_lock, downgrade_eligibility_service, storage_counts
from services.storage_counts import reducible_storage_bytes_stmt


def _sql() -> str:
    stmt = reducible_storage_bytes_stmt(uuid4())
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


def test_counts_only_what_the_owner_can_still_delete() -> None:
    sql = _sql()
    assert "sum(file_objects.size_bytes)" in sql
    assert "file_objects.status = %(status_1)s" in sql
    assert "file_objects.deleted_at IS NULL" in sql
    # Workspace-scoped files, or files of a live context — never a deleted one.
    assert "file_objects.context_id IS NULL OR file_objects.context_id IN" in sql
    assert "contexts.deleted_at IS NULL" in sql


def test_both_gates_use_the_shared_figure() -> None:
    # The gates bind the very objects storage_counts exports (an identity
    # check, not a source grep): a local re-implementation would fail here.
    assert capacity_lock.reducible_storage_bytes is storage_counts.reducible_storage_bytes
    assert (
        downgrade_eligibility_service.reducible_storage_bytes_stmt
        is storage_counts.reducible_storage_bytes_stmt
    )
    # And neither keeps a private sum of file_objects any more.
    for module in (capacity_lock, downgrade_eligibility_service):
        assert "func.sum(FileObject.size_bytes)" not in inspect.getsource(module)
