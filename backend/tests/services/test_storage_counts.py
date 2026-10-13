"""#1961: the reducible-storage figure is one statement, bound by both gates.

Row selection is pinned against PostgreSQL in
``tests/integration/test_capacity_lock_db.py`` (uploaded / reserved / failed /
soft-deleted rows, a deleted context, a workspace-scoped file, another member's
private context); this file only pins that the two gates share the object.
"""

from __future__ import annotations

from services import capacity_lock, downgrade_eligibility_service
from services.storage_counts import reducible_storage_bytes, reducible_storage_bytes_stmt


def test_both_gates_bind_the_shared_figure() -> None:
    # Identity, not a source grep: a local re-implementation would fail here.
    assert capacity_lock.reducible_storage_bytes is reducible_storage_bytes
    assert (
        downgrade_eligibility_service.reducible_storage_bytes_stmt is reducible_storage_bytes_stmt
    )
