"""Registration checks the server-owned limit before requesting account proof."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import passkeys as pk
from utils.exceptions import ConflictError


@pytest.mark.parametrize("operation", ["proof", "registration"])
async def test_full_account_is_refused_before_proof(monkeypatch, operation):
    monkeypatch.setattr(pk, "_LIMIT", 2)
    keys = [SimpleNamespace(id="first"), SimpleNamespace(id="second")]
    monkeypatch.setattr(pk, "_keys", AsyncMock(return_value=keys))
    monkeypatch.setattr(pk, "_owner", AsyncMock(return_value=SimpleNamespace(user_id="u")))
    prove, issue = AsyncMock(), AsyncMock()
    monkeypatch.setattr(pk, "_prove", prove)
    monkeypatch.setattr(pk, "_issue", issue)
    with pytest.raises(ConflictError) as exc:
        if operation == "proof":
            await pk.proof_options(MagicMock(), {"user_id": "u"}, db=AsyncMock())
        else:
            await pk.register_options(pk.Proof(), MagicMock(), {"user_id": "u"}, AsyncMock())
    assert exc.value.details["reason"] == "passkey_limit"
    assert exc.value.details["limit"] == 2
    prove.assert_not_awaited()
    issue.assert_not_awaited()


async def test_list_option_reports_server_limit_without_breaking_array_clients(monkeypatch):
    monkeypatch.setattr(pk, "_LIMIT", 3)
    monkeypatch.setattr(pk, "_keys", AsyncMock(return_value=[]))
    assert await pk.list_passkeys({"user_id": "u"}, AsyncMock()) == []
    assert await pk.list_passkeys({"user_id": "u"}, AsyncMock(), include_limit=True) == {
        "keys": [],
        "limit": 3,
    }


def test_http_list_includes_keys_and_limit(monkeypatch):
    from datetime import datetime
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from api.main import app
    from auth.dependencies import require_session_auth
    from db.base import get_db

    key = SimpleNamespace(
        id=uuid4(), name="Example key", created_at=datetime(2030, 1, 1), last_used_at=None
    )
    monkeypatch.setattr(pk, "_keys", AsyncMock(return_value=[key]))
    monkeypatch.setattr(pk, "_LIMIT", 2)
    monkeypatch.setitem(app.dependency_overrides, require_session_auth, lambda: {"user_id": "u"})
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: MagicMock())
    response = TestClient(app).get("/api/v1/auth/passkeys?include_limit=true")
    assert response.status_code == 200
    assert response.json()["limit"] == 2
    assert response.json()["keys"][0]["id"] == str(key.id)
