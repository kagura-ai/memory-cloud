"""API keys expire by default (#1537).

``APIKeyManager.create_key`` is the single lever: an omitted ``expires_days``
(``None``) resolves to the deployment default — 365 days for workspace keys,
90 days for agent-bound keys — and ``0`` is the explicit opt-in for a key that
never expires. Every mint path goes through here, so this pins the policy
once. DB access is mocked.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.api_keys import APIKeyManager
from utils.datetime import utcnow

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


def _manager(execute_rows: list) -> APIKeyManager:
    db = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=r)) for r in execute_rows]
    )
    return APIKeyManager(db)


def _settings(default: int = 365, agent: int = 90) -> SimpleNamespace:
    return SimpleNamespace(
        api_key_default_expires_days=default,
        api_key_agent_default_expires_days=agent,
    )


def _active_agent() -> SimpleNamespace:
    return SimpleNamespace(id=AGENT_ID, workspace_id=WORKSPACE_ID, status="active")


def _days_until(expires_at) -> int:
    return round((expires_at - utcnow()).total_seconds() / 86400)


@pytest.fixture
def encryptor():
    fake = MagicMock()
    fake.encrypt = MagicMock(return_value=b"encrypted")
    with patch("utils.encryption.get_encryptor", return_value=fake):
        yield fake


class TestDefaultExpiry:
    @pytest.mark.asyncio
    async def test_omitted_expiry_defaults_to_365_days(self, encryptor):
        manager = _manager([None])  # name-uniqueness lookup → no clash
        with patch("auth.api_keys.get_settings", return_value=_settings()):
            _, key = await manager.create_key(name="k", user_id="u", workspace_id=WORKSPACE_ID)
        assert key.expires_at is not None
        assert _days_until(key.expires_at) == 365

    @pytest.mark.asyncio
    async def test_agent_bound_key_defaults_to_90_days(self, encryptor):
        manager = _manager([_active_agent(), None])  # agent lookup, then uniqueness
        with patch("auth.api_keys.get_settings", return_value=_settings()):
            _, key = await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, agent_id=AGENT_ID
            )
        assert _days_until(key.expires_at) == 90

    @pytest.mark.asyncio
    async def test_explicit_expiry_wins_over_the_default(self, encryptor):
        manager = _manager([None])
        with patch("auth.api_keys.get_settings", return_value=_settings()):
            _, key = await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, expires_days=30
            )
        assert _days_until(key.expires_at) == 30

    @pytest.mark.asyncio
    async def test_zero_is_the_explicit_never_expires_opt_in(self, encryptor):
        manager = _manager([None])
        with patch("auth.api_keys.get_settings", return_value=_settings()):
            _, key = await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, expires_days=0
            )
        assert key.expires_at is None

    @pytest.mark.asyncio
    async def test_operator_settings_drive_the_defaults(self, encryptor):
        manager = _manager([None])
        with patch("auth.api_keys.get_settings", return_value=_settings(default=30, agent=7)):
            _, key = await manager.create_key(name="k", user_id="u", workspace_id=WORKSPACE_ID)
        assert _days_until(key.expires_at) == 30

        manager = _manager([_active_agent(), None])
        with patch("auth.api_keys.get_settings", return_value=_settings(default=30, agent=7)):
            _, key = await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, agent_id=AGENT_ID
            )
        assert _days_until(key.expires_at) == 7
