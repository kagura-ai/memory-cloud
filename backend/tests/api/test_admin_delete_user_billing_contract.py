"""Admin deletion must not bypass required ownership transfer (#2372).

The old destructive billing override is replaced by transfer; forced account
 erasure remains the separate, audited operator override.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.admin import delete_user


@pytest.mark.asyncio
async def test_missing_successor_rolls_back_without_personal_cleanup():
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    with (
        patch(
            "services.system_admin_service.SystemAdminService.can_delete_admin",
            AsyncMock(return_value=(True, "")),
        ),
        patch(
            "services.user_deletion_service.transfer_user_ownership",
            AsyncMock(side_effect=HTTPException(409, "Ownership transfer required")),
        ),
        patch("services.user_deletion_service.delete_user_rows", AsyncMock()) as cleanup,
    ):
        with pytest.raises(HTTPException) as error:
            await delete_user("target", {"user_id": "operator"}, db)
    assert error.value.status_code == 409
    cleanup.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
