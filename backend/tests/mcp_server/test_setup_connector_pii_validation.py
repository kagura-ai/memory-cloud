"""MCP setup_connector pii_guardrail_config validation (#866, F6-d follow-up).

The MCP provision path must reject a malformed pii_guardrail_config with a
validation_error before reaching the provisioning service — mirroring the REST
path. Both call-sites share models.schemas.validate_pii_guardrail_config.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.resource import handle_setup_connector


def _fake_get_db():
    async def _gen():
        db = MagicMock()
        db.rollback = AsyncMock()
        db.commit = AsyncMock()
        yield db

    return _gen()


@pytest.mark.asyncio
async def test_setup_connector_rejects_invalid_pii_guardrail_config():
    workspace_id = uuid4()
    args = {
        "connector_type": "slack",
        "resource_id": "slack_general",
        "pii_guardrail_config": {"enabled": True, "detector": ["EMAIL_ADDRESS"]},  # typo
    }

    with (
        patch("db.base.get_db", side_effect=lambda: _fake_get_db()),
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
