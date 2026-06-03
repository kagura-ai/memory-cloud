"""Tests for the ai-worker config endpoint (Spec 2026-06-02, Plan 3)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.workers import get_worker_config, verify_worker_token


def _settings(token: str = "wt-secret", mcp_url: str = "https://mcp.example/mcp"):
    return SimpleNamespace(worker_service_token=token, kmc_mcp_url=mcp_url)


@pytest.mark.asyncio
async def test_verify_worker_token_accepts_matching_bearer():
    with patch("api.routes.workers.get_settings", return_value=_settings()):
        # Returns None (no raise) on a valid token.
        assert await verify_worker_token("Bearer wt-secret") is None


@pytest.mark.asyncio
async def test_verify_worker_token_rejects_wrong_token():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings()):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token("Bearer nope")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_worker_token_503_when_unconfigured():
    from fastapi import HTTPException

    with patch("api.routes.workers.get_settings", return_value=_settings(token="")):
        with pytest.raises(HTTPException) as exc:
            await verify_worker_token("Bearer anything")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_get_worker_config_returns_secrets_for_ready_connector():
    db = MagicMock()
    conn = MagicMock()
    conn.id = uuid4()
    conn.workspace_id = uuid4()
    conn.context_id = uuid4()
    conn.connector_type = "slack"
    conn.locale = "ja"
    conn.external_team_id = "T01"
    conn.channel_ids = ["C01"]
    conn.pii_guardrail_config = {"enabled": True}
    conn.get_oauth_tokens.return_value = {
        "bot_token": "xoxb-x",
        "installing_admin_user_id": "U01",
    }
    conn.get_kmc_api_key.return_value = "kagura_writekey"
    conn.get_llm_config.return_value = {"provider": "anthropic", "model": "m", "api_key": "sk"}

    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
    ):
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        result = await get_worker_config(platform="slack", team_id="T01", _=None, db=db)

    assert result.connector_id == conn.id
    assert result.slack["bot_token"] == "xoxb-x"
    assert result.slack["team_id"] == "T01"
    assert result.slack["channel_ids"] == ["C01"]
    assert result.kmc == {"mcp_url": "https://mcp.example/mcp", "api_key": "kagura_writekey"}
    assert result.llm["api_key"] == "sk"
    svc.return_value.get_connector_for_dispatch.assert_awaited_once_with(
        connector_type="slack", external_team_id="T01"
    )


@pytest.mark.asyncio
async def test_get_worker_config_404_when_not_found():
    from fastapi import HTTPException

    db = MagicMock()
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
    ):
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await get_worker_config(platform="slack", team_id="TX", _=None, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_worker_config_404_when_context_not_ready():
    from fastapi import HTTPException

    db = MagicMock()
    conn = MagicMock()
    conn.context_id = None  # registration incomplete
    with (
        patch("api.routes.workers.get_settings", return_value=_settings()),
        patch("api.routes.workers.ConnectorProvisioningService") as svc,
    ):
        svc.return_value.get_connector_for_dispatch = AsyncMock(return_value=conn)
        with pytest.raises(HTTPException) as exc:
            await get_worker_config(platform="slack", team_id="T01", _=None, db=db)
    assert exc.value.status_code == 404
