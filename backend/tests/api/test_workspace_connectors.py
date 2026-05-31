"""Tests for workspace connector setup API (Issue #851, F6-b of #755)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.workspace_connectors import (
    WorkspaceConnectorCreateRequest,
    create_workspace_connector,
)
from utils.exceptions import MemoryCloudException


@pytest.mark.asyncio
async def test_create_workspace_connector_rolls_back_on_service_failure():
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch(
        "api.routes.workspace_connectors.ConnectorProvisioningService"
    ) as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(
            side_effect=MemoryCloudException(
                "Connector seat limit reached.",
                status_code=403,
                error_code="CONNECTOR-SEAT-CAP",
            )
        )
        with pytest.raises(MemoryCloudException):
            await create_workspace_connector(request, admin, db)

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()

