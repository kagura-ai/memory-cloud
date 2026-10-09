"""Admin DELETE /admin/users/{id} logs the subscriptions it overrides (Issue #1940).

An admin delete is the operator's escape hatch from the running-subscription
block, so it is not refused — but it must leave the same audit event as admin
force-erase, naming the workspaces whose billing needs reconciling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.admin import delete_user


def _db() -> MagicMock:
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = SimpleNamespace(email="t@example.com")
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


async def _call(under_contract: list) -> tuple[MagicMock, MagicMock]:
    with (
        patch("services.system_admin_service.SystemAdminService") as admin_svc,
        patch(
            "services.identity_link_service.hand_over_private_contexts",
            new=AsyncMock(),
        ),
        patch("services.file_storage_service.FileStorageService") as storage,
        patch(
            "services.billing_contract.owned_workspaces_under_contract",
            new=AsyncMock(return_value=under_contract),
        ),
        patch("api.routes.admin.logger") as mock_logger,
    ):
        admin_svc.return_value.can_delete_admin = AsyncMock(return_value=(True, None))
        storage.return_value.purge_files_for_contexts = AsyncMock(return_value={})
        db = _db()
        await delete_user("target-1", {"user_id": "admin-1"}, db)
    return mock_logger, db


def _override_calls(mock_logger: MagicMock) -> list:
    return [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == "erasure_admin_override_billing_contract"
    ]


@pytest.mark.asyncio
async def test_logs_workspaces_under_contract():
    ws = SimpleNamespace(id=uuid4())
    mock_logger, db = await _call([ws])
    calls = _override_calls(mock_logger)
    assert len(calls) == 1
    assert calls[0].kwargs["workspace_ids"] == [str(ws.id)]
    assert calls[0].kwargs["user_id"] == "target-1"
    # Not refused: the user row is still deleted and committed.
    db.delete.assert_awaited_once()
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_no_log_without_contract():
    mock_logger, db = await _call([])
    assert _override_calls(mock_logger) == []
    db.delete.assert_awaited_once()
