"""#1961: the reducible-storage figure is one statement, used by both gates."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.dialects import postgresql

from services import capacity_lock, downgrade_eligibility_service, storage_counts
from services.storage_counts import reducible_storage_bytes_stmt


def _sql() -> str:
    stmt = reducible_storage_bytes_stmt(uuid4())
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return " ".join(str(compiled).split())


def test_counts_only_what_the_workspace_can_still_shed() -> None:
    sql = _sql()
    assert "sum(file_objects.size_bytes)" in sql
    assert "file_objects.status = 'uploaded'" in sql
    assert "file_objects.deleted_at IS NULL" in sql
    # Workspace-scoped files, or files of a live context of THIS workspace.
    assert "file_objects.context_id IS NULL OR file_objects.context_id IN" in sql
    assert "contexts.workspace_id = " in sql
    assert "contexts.deleted_at IS NULL" in sql


def test_both_gates_bind_the_shared_figure() -> None:
    # Identity, not a source grep: a local re-implementation would fail here.
    assert capacity_lock.reducible_storage_bytes is storage_counts.reducible_storage_bytes
    assert (
        downgrade_eligibility_service.reducible_storage_bytes_stmt
        is storage_counts.reducible_storage_bytes_stmt
    )
