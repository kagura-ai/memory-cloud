"""MCP setup_connector pii_guardrail_config validation (#866, F6-d follow-up).

The MCP provision path must reject a malformed pii_guardrail_config with a
validation_error before reaching the provisioning service — mirroring the REST
path. Both call-sites share models.schemas.validate_pii_guardrail_config.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.resource import handle_setup_connector


def _fake_get_db():
    async def _gen():
        db = MagicMock()
        db.rollback = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        yield db

    return _gen()


def test_setup_connector_schema_documents_only_tenant_owned_runtime_controls():
    setup = next(tool for tool in get_tool_definitions() if tool["name"] == "setup_connector")
    runtime = setup["inputSchema"]["properties"]["runtime"]

    assert runtime["additionalProperties"] is False
    assert runtime["properties"]["buffer"]["additionalProperties"] is False
    assert "ttl_seconds" in runtime["properties"]["buffer"]["properties"]
    assert "redis_url" not in runtime["properties"]["buffer"]["properties"]
    assert "vision_enabled" in runtime["properties"]


@pytest.mark.asyncio
async def test_setup_connector_rejects_invalid_pii_guardrail_config():
    workspace_id = uuid4()
    args = {
        "connector_type": "slack",
        "resource_id": "slack_general",
        "pii_guardrail_config": {"enabled": True, "detector": ["EMAIL_ADDRESS"]},  # typo
    }

    with (
        patch("db.base.get_db", side_effect=_fake_get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.connector_provisioning.ConnectorProvisioningService") as service_cls,
    ):
        service_cls.return_value.provision_connector = AsyncMock()
        result = await handle_setup_connector(args, "user-1", workspace_id)

    payload = json.loads(result[0].text)
    assert payload.get("error") == "validation_error"
    service_cls.return_value.provision_connector.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_connector_rejects_tenant_controlled_redis_url():
    workspace_id = uuid4()
    args = {
        "connector_type": "slack",
        "resource_id": "slack_general",
        "runtime": {"buffer": {"redis_url": "redis://tenant.invalid:6379/0"}},
    }

    with (
        patch("db.base.get_db", side_effect=_fake_get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.connector_provisioning.ConnectorProvisioningService") as service_cls,
    ):
        service_cls.return_value.provision_connector = AsyncMock()
        result = await handle_setup_connector(args, "user-1", workspace_id)

    payload = json.loads(result[0].text)
    assert payload.get("error") == "validation_error"
    service_cls.return_value.provision_connector.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_connector_passes_normalized_runtime_to_service():
    workspace_id = uuid4()
    connector_id = uuid4()
    args = {
        "connector_type": "slack",
        "resource_id": "slack_general",
        "runtime": {"vision_enabled": False},
    }
    provisioned = SimpleNamespace(
        connector=SimpleNamespace(id=connector_id, connector_type="slack"),
        token=SimpleNamespace(id=7, quota_events_per_hour=1000),
        resource_id="slack_general",
        resource_pk=uuid4(),
        context_id=None,
        plaintext_token="resource-token",
        plaintext_kmc_api_key=None,
    )

    with (
        patch("db.base.get_db", side_effect=_fake_get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.connector_provisioning.ConnectorProvisioningService") as service_cls,
        patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
    ):
        service_cls.return_value.provision_connector = AsyncMock(return_value=provisioned)
        result = await handle_setup_connector(args, "user-1", workspace_id)

    payload = json.loads(result[0].text)
    assert payload.get("connector_id") == str(connector_id)
    runtime = service_cls.return_value.provision_connector.await_args.kwargs["runtime_config"]
    assert runtime["vision_enabled"] is False
    assert runtime["buffer"] == {"ttl_seconds": 86400, "max_len": 10_000}
