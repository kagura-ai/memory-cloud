"""History attribution follows verified authentication, never client claims."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from api.correlation import CorrelationContext, set_correlation
from auth.api_keys import VerifiedKey
from auth.history_principal import get_history_principal, set_history_principal
from services.operation_history_writer import record_operation


@pytest.fixture(autouse=True)
def attribution_scope():
    set_history_principal()
    set_correlation(None)
    yield
    set_history_principal()
    set_correlation(None)


def write(**kwargs):
    return record_operation(
        AsyncSession(),
        workspace_id=uuid4(),
        actor_user_id="account-owner",
        operation="memory.created",
        **kwargs,
    )


@pytest.mark.parametrize(
    "kind,transport,surface",
    [
        ("session", "rest", "web"),
        ("api_key", "rest", "api"),
        ("oauth", "rest", "api"),
        ("session", "mcp", "mcp"),
        ("api_key", "mcp", "mcp"),
        ("system", "mcp", "system"),
        ("unknown", "rest", "unknown"),
    ],
)
def test_verified_authentication_selects_surface(kind, transport, surface):
    set_correlation(CorrelationContext("a" * 32, "b" * 16, surface=transport))
    set_history_principal(kind)
    row = write()
    assert row.surface == surface
    assert row.principal_kind == kind


def test_shared_key_records_public_identity_and_explicit_agent_none(monkeypatch):
    from services.write_provenance import WriteProvenance

    monkeypatch.setattr(
        "services.operation_history_writer.current_provenance", lambda: WriteProvenance(uuid4())
    )
    set_history_principal("api_key", credential_id="akey_" + "a" * 22)
    row = write(agent_id=None)
    assert row.agent_id is None
    assert row.credential_id == "akey_" + "a" * 22
    assert row.actor_user_id == "account-owner"  # account identity, not the physical operator
    system = write(surface="system")
    assert system.actor_user_id is system.credential_id is system.agent_id is None
    assert system.principal_kind == "system"


@pytest.mark.parametrize(
    "at",
    [datetime(2026, 11, 9, 12), datetime(2026, 11, 9, 21, tzinfo=timezone(timedelta(hours=9)))],
)
def test_naive_source_times_are_utc_not_process_local(at):
    assert write(at=at).occurred_at == datetime(2026, 11, 9, 12, tzinfo=UTC)


def test_models_package_registers_history_without_importing_api_or_writer():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'src'); from models.auth import Base; "
            "assert 'workspace_operation_history' in Base.metadata.tables",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_real_rest_auth_adapters_replace_stale_principal(monkeypatch):
    from auth.dependencies import get_current_user, get_user_from_api_key_or_session

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/test", "headers": []})
    request.state.user = {"user_id": "browser"}
    set_history_principal("api_key", credential_id="stale")
    assert await get_current_user(request) == {"user_id": "browser"}
    assert get_history_principal().kind == "session"
    assert get_history_principal().credential_id is None

    key = VerifiedKey(1, "owner", uuid4(), None, public_id="akey_" + "a" * 22)
    monkeypatch.setattr("auth.api_keys.APIKeyManager.verify_key", AsyncMock(return_value=key))
    await get_user_from_api_key_or_session(request, "kagura_test", MagicMock())
    assert get_history_principal().kind == "api_key"
    assert get_history_principal().credential_id == key.public_id


@pytest.mark.asyncio
async def test_oauth_attribution_is_the_registered_client_not_token(monkeypatch):
    from auth.oauth2_bearer import verify_oauth_bearer_token

    token = SimpleNamespace(user_id="owner", scope="memory:write", client_id="registered-client")
    monkeypatch.setattr("auth.oauth2_bearer.find_active_oauth_token", AsyncMock(return_value=token))
    assert await verify_oauth_bearer_token("opaque-secret", MagicMock()) == (
        "owner",
        "memory:write",
    )
    row = write()
    assert row.client_id == "registered-client"
    assert row.credential_id is None
    assert "opaque-secret" not in repr(row.__dict__)
