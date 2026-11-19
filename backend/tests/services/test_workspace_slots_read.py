"""The slot read helpers run no statement unless the mode is ``enforce`` (#2918).

With ``WORKSPACE_SLOT_SUSPENSION`` ``off`` the code must be deployable before
the migration, and in ``shadow`` the gates ignore the rows. The rule itself
(stamped owner, live workspace, no billing contract) is pinned against a real
database in ``tests/integration/test_workspace_slots_db.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.workspace_slots import owner_selection_required, suspended_workspace_ids


def _mode(mode: str):
    return patch(
        "config.settings.get_settings",
        return_value=SimpleNamespace(workspace_slot_suspension=mode),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_no_statement_unless_enforced(mode: str) -> None:
    db = MagicMock()
    db.execute = AsyncMock()
    with _mode(mode):
        assert await suspended_workspace_ids(db, [uuid4(), uuid4()]) == frozenset()
        assert await owner_selection_required(db, "owner-1") is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ids_no_statement_when_enforced() -> None:
    db = MagicMock()
    db.execute = AsyncMock()
    with _mode("enforce"):
        assert await suspended_workspace_ids(db, []) == frozenset()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforced_lookup_is_one_statement() -> None:
    wid = uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [wid]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    with _mode("enforce"):
        assert await suspended_workspace_ids(db, [wid, wid, uuid4()]) == frozenset({wid})
    assert db.execute.await_count == 1
