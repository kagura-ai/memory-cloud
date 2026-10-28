"""The admin route commits shared cleanup before any post-commit work."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.admin import delete_user
from services.user_deletion_service import UserDeletionResult


@pytest.mark.asyncio
async def test_response_reports_moved_and_deleted_keys_after_commit():
    db = MagicMock()
    order = []
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    db.rollback = AsyncMock()
    result = UserDeletionResult(memories=4, api_keys=3, connector_api_keys=2)

    async def finish(_result):
        assert order == ["commit"]

    with (
        patch(
            "services.system_admin_service.SystemAdminService.can_delete_admin",
            AsyncMock(return_value=(True, "")),
        ),
        patch("services.user_deletion_service.transfer_user_ownership", AsyncMock(return_value={})),
        patch("services.user_deletion_service.delete_user_rows", AsyncMock(return_value=result)),
        patch("services.user_deletion_service.finish_user_deletion", side_effect=finish),
    ):
        response = await delete_user("target", {"user_id": "operator"}, db)
    assert response["deleted"] == {"memories": 4, "api_keys": 3, "contexts": 0}
    assert response["reowned"] == {"connector_api_keys": 2}
    db.rollback.assert_not_awaited()
