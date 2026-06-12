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
    assert exc.value.error_code == "VAL-001"  # canonical validation code, not a one-off
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
    result.context_id = None
    result.plaintext_kmc_api_key = None
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


@pytest.mark.asyncio
async def test_list_workspace_connectors_returns_summaries():
    from datetime import datetime

    from api.routes.workspace_connectors import list_workspace_connectors

    db = MagicMock()
    ws_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}

    c = MagicMock()
    c.id = uuid4()
    c.connector_type = "slack"
    c.resource_pk = uuid4()
    c.context_id = uuid4()
    c.config_version = 1
    c.created_at = datetime(2026, 6, 2, 0, 0, 0)
    c.created_by = "user-1"

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.list_connectors = AsyncMock(return_value=[c])
        result = await list_workspace_connectors(admin, db)

    assert len(result) == 1
    assert result[0].connector_id == c.id
    assert result[0].connector_type == "slack"
    service_cls.return_value.list_connectors.assert_awaited_once_with(ws_id)


@pytest.mark.asyncio
async def test_list_workspace_connectors_400_without_workspace():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import list_workspace_connectors

    db = MagicMock()
    admin = {"user_id": "user-1", "current_workspace_id": None}

    with pytest.raises(HTTPException) as exc:
        await list_workspace_connectors(admin, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_workspace_connector_204_on_success():
    from api.routes.workspace_connectors import delete_workspace_connector

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    ws_id = uuid4()
    conn_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.delete_connector = AsyncMock(return_value=True)
        resp = await delete_workspace_connector(conn_id, admin, db)

    assert resp.status_code == 204
    db.commit.assert_awaited_once()
    service_cls.return_value.delete_connector.assert_awaited_once_with(ws_id, conn_id)


@pytest.mark.asyncio
async def test_delete_workspace_connector_404_when_missing():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import delete_workspace_connector

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.delete_connector = AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as exc:
            await delete_workspace_connector(uuid4(), admin, db)

    assert exc.value.status_code == 404
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


# --- rotate-kmc-key (#892) ---


@pytest.mark.asyncio
async def test_rotate_kmc_key_returns_new_key_on_success():
    from datetime import datetime

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from services.connector_provisioning import KmcKeyRotationResult

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    ws_id = uuid4()
    conn_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}
    expires = datetime(2099, 1, 1)

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            return_value=KmcKeyRotationResult(
                plaintext_kmc_api_key="kmc-new-plaintext",
                expires_at=expires,
                config_version=3,
            )
        )
        resp = await rotate_connector_kmc_key(conn_id, admin, db)

    assert resp.connector_id == conn_id
    assert resp.kmc_api_key == "kmc-new-plaintext"
    assert resp.kmc_api_key_expires_at == expires
    assert resp.config_version == 3
    db.commit.assert_awaited_once()
    service_cls.return_value.rotate_kmc_key.assert_awaited_once_with(
        workspace_id=ws_id, connector_id=conn_id, user_id="user-1"
    )


@pytest.mark.asyncio
async def test_rotate_kmc_key_404_when_connector_missing():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            side_effect=NotFoundException("Connector", str(uuid4()))
        )
        with pytest.raises(HTTPException) as exc:
            await rotate_connector_kmc_key(uuid4(), admin, db)

    assert exc.value.status_code == 404
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotate_kmc_key_422_when_no_kmc_key():
    from fastapi import HTTPException

    from api.routes.workspace_connectors import rotate_connector_kmc_key
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.rotate_kmc_key = AsyncMock(
            side_effect=ValidationError("Connector has no KMC write key")
        )
        with pytest.raises(HTTPException) as exc:
            await rotate_connector_kmc_key(uuid4(), admin, db)

    assert exc.value.status_code == 422
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
