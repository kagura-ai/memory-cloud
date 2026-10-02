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
        token=SimpleNamespace(id=7, public_id="rtok_" + "7" * 22, quota_events_per_hour=1000),
        resource_id="slack_general",
        resource_pk=uuid4(),
        context_id=None,
        plaintext_token="resource-token",
        plaintext_kmc_api_key=None,
        kmc_api_key_name=None,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quota_events_per_hour", "lots"),
        ("quota_events_per_hour", None),
        ("virtual_key_valid_until", "next tuesday"),
        ("virtual_key_valid_until", 1700000000),
    ],
)
async def test_setup_connector_names_the_field_it_cannot_parse(field, value):
    """#1742: the raw int() / fromisoformat() text named neither the field nor
    the expected format."""
    args = {"connector_type": "slack", "resource_id": "slack_general", field: value}

    with (
        patch("db.base.get_db", side_effect=_fake_get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.connector_provisioning.ConnectorProvisioningService") as service_cls,
    ):
        service_cls.return_value.provision_connector = AsyncMock()
        result = await handle_setup_connector(args, "user-1", uuid4())

    payload = json.loads(result[0].text)
    assert payload["error"] == "validation_error"
    assert payload["field"] == field
    assert payload["message"].startswith(field)
    assert "invalid literal" not in payload["message"]
    assert "Invalid isoformat" not in payload["message"]
    service_cls.return_value.provision_connector.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key_name", ["connector:abc", None])
async def test_setup_connector_notifies_when_a_kmc_key_was_minted(key_name):
    """#1752: a minted KMC write key emails its owner (the caller); the key
    value never reaches the notice."""
    provisioned = SimpleNamespace(
        connector=SimpleNamespace(id=uuid4(), connector_type="slack"),
        token=SimpleNamespace(id=7, public_id="rtok_" + "7" * 22, quota_events_per_hour=1000),
        resource_id="slack_general",
        resource_pk=uuid4(),
        context_id=uuid4() if key_name else None,
        plaintext_token="resource-token",
        plaintext_kmc_api_key="kagura_SECRETVALUE" if key_name else None,
        kmc_api_key_name=key_name,
    )
    spawn = MagicMock()
    with (
        patch("db.base.get_db", side_effect=_fake_get_db),
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=None),
        ),
        patch("services.connector_provisioning.ConnectorProvisioningService") as service_cls,
        patch("mcp_server.tools.resource._log_tool_usage", new=AsyncMock()),
        patch("services.security_notification_service.spawn_security_notification", spawn),
    ):
        service_cls.return_value.provision_connector = AsyncMock(return_value=provisioned)
        await handle_setup_connector(
            {"connector_type": "slack", "resource_id": "slack_general"}, "user-1", uuid4()
        )

    if key_name is None:
        spawn.assert_not_called()
        return
    kwargs = spawn.call_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["event"] == "api_key_created"
    assert kwargs["key_name"] == "connector:abc"
    assert "kagura_SECRETVALUE" not in repr(kwargs)
