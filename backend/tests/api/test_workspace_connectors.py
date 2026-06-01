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

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
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


@pytest.mark.asyncio
async def test_create_rejects_invalid_pii_guardrail_config_before_calling_service():
    # #866: a malformed pii_guardrail_config (typo'd key) must be rejected at the
    # provision path with a 422, before the service / DB is touched.
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        pii_guardrail_config={"enabled": True, "detector": ["EMAIL_ADDRESS"]},  # typo: detector
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock()
        with pytest.raises(MemoryCloudException) as exc:
            await create_workspace_connector(request, admin, db)

    assert exc.value.status_code == 422
    service_cls.return_value.provision_connector.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_passes_normalized_pii_guardrail_config_dict_to_service():
    # Valid config is normalized (defaults materialized) and handed to the service
    # as a plain dict for JSONB storage — not a Pydantic model.
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        pii_guardrail_config={"enabled": True, "detectors": ["EMAIL_ADDRESS"]},
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    result = MagicMock()
    result.connector.id = uuid4()
    result.connector.connector_type = "slack"
    result.resource_id = "slack_general"
    result.resource_pk = uuid4()
    result.token.id = 1
    result.plaintext_token = "kagura_resource_x"
    result.token.quota_events_per_hour = 1000

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(return_value=result)
        await create_workspace_connector(request, admin, db)

    kwargs = service_cls.return_value.provision_connector.await_args.kwargs
    assert kwargs["pii_guardrail_config"] == {
        "enabled": True,
        "detectors": ["EMAIL_ADDRESS"],
        "redaction": "mask",
        "locale": "en",
        "fail_closed": True,
    }
