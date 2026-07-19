"""Tests for workspace connector setup API (Issue #851, F6-b of #755)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

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
    result.connector.app_key = "default"
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
async def test_create_passes_normalized_runtime_config_to_service():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    request = WorkspaceConnectorCreateRequest(
        connector_type="slack",
        resource_id="slack_general",
        runtime={"vision_enabled": False},
    )
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    result = MagicMock()
    result.connector.id = uuid4()
    result.connector.connector_type = "slack"
    result.connector.app_key = "default"
    result.resource_id = "slack_general"
    result.context_id = None
    result.plaintext_kmc_api_key = None
    result.token.id = 1
    result.plaintext_token = "kagura_resource_x"
    result.token.quota_events_per_hour = 1000

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.provision_connector = AsyncMock(return_value=result)
        await create_workspace_connector(request, admin, db)

    runtime = service_cls.return_value.provision_connector.await_args.kwargs["runtime_config"]
    assert runtime["vision_enabled"] is False
    assert runtime["buffer"] == {"ttl_seconds": 86400, "max_len": 10_000}


def test_create_rejects_process_owned_runtime_fields_at_rest_boundary():
    with pytest.raises(PydanticValidationError):
        WorkspaceConnectorCreateRequest(
            connector_type="slack",
            resource_id="slack_general",
            runtime={"buffer": {"redis_url": "redis://tenant.invalid:6379/0"}},
        )


@pytest.mark.asyncio
async def test_update_runtime_commits_revision_and_returns_normalized_config():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from services.connector_provisioning import ConnectorRuntimeUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    connector_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_runtime_config = AsyncMock(
            return_value=ConnectorRuntimeUpdateResult(
                runtime_config=request.runtime.model_dump(mode="json"),
                config_version=4,
            )
        )
        response = await update_workspace_connector_runtime(connector_id, request, admin, db)

    assert response.connector_id == connector_id
    assert response.runtime.vision_enabled is False
    assert response.stored is True
    assert response.config_version == 4
    db.commit.assert_awaited_once()
    service_cls.return_value.update_runtime_config.assert_awaited_once_with(
        workspace_id=workspace_id,
        connector_id=connector_id,
        runtime_config=request.runtime.model_dump(mode="json"),
        user_id="user-1",
        expected_config_version=None,
    )


@pytest.mark.asyncio
async def test_update_runtime_hides_cross_workspace_connector_as_not_found():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import NotFoundException

    db = MagicMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_runtime_config = AsyncMock(
            side_effect=NotFoundException("Connector", "hidden")
        )
        with pytest.raises(NotFoundException) as exc:
            await update_workspace_connector_runtime(
                uuid4(),
                WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
                admin,
                db,
            )

    assert exc.value.status_code == 404
    assert exc.value.message == "Connector not found"
    assert "hidden" not in exc.value.message
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_runtime_requires_selected_workspace():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import BadRequestError

    with pytest.raises(BadRequestError) as exc:
        await update_workspace_connector_runtime(
            uuid4(),
            WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
            {"user_id": "user-1", "current_workspace_id": None},
            MagicMock(),
        )

    assert exc.value.status_code == 400
    assert exc.value.error_code == "REQ-001"


@pytest.mark.asyncio
async def test_update_runtime_maps_unexpected_failure_to_canonical_internal_error():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorRuntimeUpdateRequest,
        update_workspace_connector_runtime,
    )
    from utils.exceptions import InternalError

    db = MagicMock()
    db.rollback = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls,
        patch("api.routes.workspace_connectors.logger.error") as log_error,
    ):
        service_cls.return_value.update_runtime_config = AsyncMock(
            side_effect=RuntimeError("secret-bearing internal detail")
        )
        with pytest.raises(InternalError) as exc:
            await update_workspace_connector_runtime(
                uuid4(),
                WorkspaceConnectorRuntimeUpdateRequest(runtime={"vision_enabled": False}),
                admin,
                db,
            )

    assert exc.value.status_code == 500
    assert exc.value.error_code == "INT-001"
    assert exc.value.message == "Failed to update connector runtime"
    assert "secret-bearing" not in str(log_error.call_args)
    assert log_error.call_args.kwargs["error_type"] == "RuntimeError"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_workspace_connectors_returns_summaries():
    from datetime import datetime
    from types import SimpleNamespace

    from api.routes.workspace_connectors import list_workspace_connectors

    db = MagicMock()
    ws_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": ws_id}

    c = MagicMock()
    c.id = uuid4()
    c.connector_type = "slack"
    c.app_key = "default"
    c.context_id = uuid4()
    c.config_version = 1
    c.runtime_config = None
    c.created_at = datetime(2026, 6, 2, 0, 0, 0)
    c.created_by = "user-1"
    # #1376: settings surfaced for the admin-card presence indicators.
    c.channel_ids = ["C01"]
    c.locale = "ja"
    c.litellm_virtual_key_id = None
    c.llm_config_encrypted = "ENC:x"

    # list_connectors now returns ConnectorListItem(connector, resource_id) so the
    # summary exposes the public slug, not the internal resource_pk DB key (#991).
    item = SimpleNamespace(connector=c, resource_id="my-resource-slug")

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.list_connectors = AsyncMock(return_value=[item])
        result = await list_workspace_connectors(admin, db)

    assert len(result) == 1
    assert result[0].connector_id == c.id
    assert result[0].connector_type == "slack"
    assert result[0].app_key == "default"
    assert result[0].resource_id == "my-resource-slug"
    assert not hasattr(result[0], "resource_pk")
    # #1376: presence indicators for the admin card; the LLM bundle itself is
    # write-only and must never be listed.
    assert result[0].channel_ids == ["C01"]
    assert result[0].locale == "ja"
    assert result[0].llm_config_present is True
    assert result[0].litellm_virtual_key_id is None
    assert "ENC:" not in result[0].model_dump_json()
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
async def test_list_available_worker_apps_returns_active_non_secret_metadata():
    from api.routes.workspace_connectors import list_available_worker_apps

    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    identity = MagicMock()
    identity.platform = "slack"
    identity.app_key = "sales"
    identity.display_name = "Sales Slack App"

    with patch("services.worker_app_identity.WorkerAppIdentityService") as service_cls:
        service_cls.return_value.list_identities = AsyncMock(return_value=[identity])
        result = await list_available_worker_apps(admin, MagicMock())

    assert [item.model_dump() for item in result] == [
        {
            "platform": "slack",
            "app_key": "sales",
            "display_name": "Sales Slack App",
        }
    ]
    assert "signing_secret" not in result[0].model_dump()
    service_cls.return_value.list_identities.assert_awaited_once_with(active_only=True)


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


@pytest.mark.asyncio
async def test_update_settings_route_passes_only_provided_fields():
    """#1376: absent request fields never reach the service (PATCH semantics)."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    connector_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"channel_ids": ["C01"], "expected_config_version": 3}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=["C01"],
                litellm_virtual_key_id=None,
                llm_config_present=False,
                locale=None,
                config_version=4,
            )
        )
        response = await update_workspace_connector_settings(connector_id, request, admin, db)

    assert response.connector_id == connector_id
    assert response.channel_ids == ["C01"]
    assert response.config_version == 4
    db.commit.assert_awaited_once()
    service_cls.return_value.update_connector_settings.assert_awaited_once_with(
        workspace_id=workspace_id,
        connector_id=connector_id,
        user_id="user-1",
        expected_config_version=3,
        channel_ids=["C01"],
    )


@pytest.mark.asyncio
async def test_update_settings_route_distinguishes_explicit_null_from_absent():
    """#1376: explicit null (clear) is forwarded; untouched fields are not."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    workspace_id = uuid4()
    admin = {"user_id": "user-1", "current_workspace_id": workspace_id}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"llm_config": None, "locale": None}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=["C-kept"],
                litellm_virtual_key_id="vk-kept",
                llm_config_present=False,
                locale=None,
                config_version=9,
            )
        )
        await update_workspace_connector_settings(uuid4(), request, admin, db)

    kwargs = service_cls.return_value.update_connector_settings.await_args.kwargs
    assert kwargs["llm_config"] is None
    assert kwargs["locale"] is None
    assert "channel_ids" not in kwargs
    assert "litellm_virtual_key_id" not in kwargs


@pytest.mark.asyncio
async def test_update_settings_response_never_echoes_llm_config():
    """#1376: the LLM bundle is write-only — only a presence flag comes back."""
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from services.connector_provisioning import ConnectorSettingsUpdateResult

    db = MagicMock()
    db.commit = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate(
        {"llm_config": {"provider": "openai", "model": "gpt", "api_key": "sk-secret"}}
    )

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            return_value=ConnectorSettingsUpdateResult(
                channel_ids=None,
                litellm_virtual_key_id=None,
                llm_config_present=True,
                locale=None,
                config_version=2,
            )
        )
        response = await update_workspace_connector_settings(uuid4(), request, admin, db)

    assert response.llm_config_present is True
    assert "sk-secret" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_update_settings_route_rolls_back_on_conflict():
    from api.routes.workspace_connectors import (
        WorkspaceConnectorSettingsUpdateRequest,
        update_workspace_connector_settings,
    )
    from utils.exceptions import ConflictError

    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    admin = {"user_id": "user-1", "current_workspace_id": uuid4()}
    request = WorkspaceConnectorSettingsUpdateRequest.model_validate({"channel_ids": ["C01"]})

    with patch("api.routes.workspace_connectors.ConnectorProvisioningService") as service_cls:
        service_cls.return_value.update_connector_settings = AsyncMock(
            side_effect=ConflictError("stale")
        )
        with pytest.raises(ConflictError):
            await update_workspace_connector_settings(uuid4(), request, admin, db)

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
