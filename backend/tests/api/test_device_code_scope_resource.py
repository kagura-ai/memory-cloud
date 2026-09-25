"""Device authorization grant: scope, ``resource`` and logging rules (#1686).

Runs the device flow (authorize → confirm → token polling) against an
in-memory SQLite database holding the OAuth tables, as
``test_device_code_endpoints.py`` does, and pins:

- ``/device/authorize`` grants scope by the ``/authorize`` rule, including a
  DCR client registered with only non-memory scopes;
- a device code yields one token;
- a ``resource`` on the polling request must name this server's MCP resource,
  is stored as the published identifier, and is checked before the
  authorization state;
- ``/device/confirm`` logs a ``user_code`` prefix only.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from api.main import app  # noqa: E402
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402
from models.auth import (  # noqa: E402
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
    User,
    Workspace,
)
from utils.datetime import utcnow  # noqa: E402

FRONTEND = "https://memory.example.test"
MCP_RESOURCE = f"{FRONTEND}/mcp"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT = "device-scope-test-cli"
CLI_SCOPE = "memory:read memory:write offline_access"
NO_MEMORY_CLIENT = "device-scope-test-no-memory"
LEGACY_DCR_CLIENT = "device-scope-test-dcr-claudeai"


@pytest.fixture
def db_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (User, Workspace, OAuth2Client, OAuth2DeviceCode, OAuth2Token):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    common: dict[str, Any] = {
        "client_secret_hash": "",
        "grant_types": [DEVICE_GRANT, "refresh_token"],
        "response_types": [],
        "redirect_uris": [],
        "token_endpoint_auth_method": "none",
        "provider": "claude",
    }
    with factory() as db:
        db.add_all(
            [
                OAuth2Client(
                    client_id=CLI_CLIENT, client_name="Test CLI", scope=CLI_SCOPE, **common
                ),
                # Admin-managed, registered without a memory scope.
                OAuth2Client(
                    client_id=NO_MEMORY_CLIENT,
                    client_name="Test CLI without memory scope",
                    scope="openid offline_access",
                    owner_id="admin-user",
                    **common,
                ),
                # A DCR row stored with the scope the client asked for.
                OAuth2Client(
                    client_id=LEGACY_DCR_CLIENT,
                    client_name="Test CLI registered with claudeai",
                    scope="claudeai",
                    **common,
                ),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def api(db_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FRONTEND_URL", FRONTEND)
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)
    with (
        patch("api.routes.oauth.increment_counter", AsyncMock(return_value=1)),
        patch("api.routes.oauth.get_sync_session", side_effect=db_factory),
        patch(
            "api.routes.oauth._get_user_from_session",
            return_value={"user_id": "device-user", "email": "device-user@example.test"},
        ),
    ):
        # Authlib refuses the token endpoint over plain http.
        yield TestClient(app, base_url="https://testserver")


def _authorize(api: TestClient, client_id: str = CLI_CLIENT, scope: str | None = None) -> Any:
    body = {"client_id": client_id}
    if scope is not None:
        body["scope"] = scope
    return api.post("/api/v1/oauth/device/authorize", data=body)


def _approve(api: TestClient, db_factory: sessionmaker, grant: dict[str, Any]) -> None:
    confirmed = api.post(
        "/api/v1/oauth/device/confirm",
        json={"user_code": grant["user_code"], "approve": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    # The CLI waits ``interval`` seconds between polls.
    with db_factory() as db:
        row = db.query(OAuth2DeviceCode).filter_by(user_code=grant["user_code"]).one()
        row.last_polled_at = utcnow() - timedelta(seconds=grant["interval"] + 1)
        db.commit()


def _poll(api: TestClient, grant: dict[str, Any], client_id: str = CLI_CLIENT, **extra: str) -> Any:
    return api.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": DEVICE_GRANT,
            "device_code": grant["device_code"],
            "client_id": client_id,
            **extra,
        },
    )


def _stored_scope(db_factory: sessionmaker, user_code: str) -> str | None:
    with db_factory() as db:
        return db.query(OAuth2DeviceCode).filter_by(user_code=user_code).one().scope


class TestDeviceScope:
    @pytest.mark.parametrize(
        ("requested", "granted"),
        [
            ("memory:read", "memory:read"),
            ("memory:read memory:admin", "memory:read"),
            ("memory:write undefined:scope", "memory:write"),
            # No memory scope left: the registered scope.
            ("claudeai", CLI_SCOPE),
            ("openid offline_access", CLI_SCOPE),
            ("", CLI_SCOPE),
            (None, CLI_SCOPE),
        ],
    )
    def test_granted_scope(
        self, api: TestClient, db_factory: sessionmaker, requested: str | None, granted: str
    ) -> None:
        response = _authorize(api, scope=requested)

        assert response.status_code == 200, response.text
        assert _stored_scope(db_factory, response.json()["user_code"]) == granted

    def test_client_registered_without_memory_scope_is_invalid_scope(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        response = _authorize(api, NO_MEMORY_CLIENT, scope="openid")

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_scope"
        with db_factory() as db:
            assert db.query(OAuth2DeviceCode).count() == 0


class TestDcrDeviceClientWithoutMemoryScope:
    def test_device_flow_for_a_dcr_client_registered_with_claudeai(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        response = _authorize(api, LEGACY_DCR_CLIENT, scope="claudeai")
        assert response.status_code == 200, response.text
        grant = response.json()
        assert _stored_scope(db_factory, grant["user_code"]) == DCR_DEFAULT_SCOPE
        _approve(api, db_factory, grant)

        token = _poll(api, grant, client_id=LEGACY_DCR_CLIENT)

        assert token.status_code == 200, token.text
        assert token.json()["scope"] == DCR_DEFAULT_SCOPE


class TestDeviceCodeSingleUse:
    def test_a_device_code_yields_one_token(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        grant = _authorize(api, scope="memory:read").json()
        _approve(api, db_factory, grant)
        first = _poll(api, grant)
        assert first.status_code == 200, first.text

        second = _poll(api, grant)

        assert second.status_code == 400
        with db_factory() as db:
            assert db.query(OAuth2Token).count() == 1
            assert db.query(OAuth2DeviceCode).count() == 0


class TestDeviceResource:
    @pytest.mark.parametrize(
        "resource",
        [MCP_RESOURCE, f"{MCP_RESOURCE}/", f"{MCP_RESOURCE}/w/workspace-id?profile=core"],
    )
    def test_resource_is_stored_as_published(
        self, api: TestClient, db_factory: sessionmaker, resource: str
    ) -> None:
        grant = _authorize(api, scope="memory:read").json()
        _approve(api, db_factory, grant)

        token = _poll(api, grant, resource=resource)

        assert token.status_code == 200, token.text
        with db_factory() as db:
            stored = db.query(OAuth2Token).filter_by(access_token=token.json()["access_token"])
            assert stored.one().resource == MCP_RESOURCE

    def test_no_resource_issues_no_audience(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        grant = _authorize(api, scope="memory:read").json()
        _approve(api, db_factory, grant)

        token = _poll(api, grant)

        assert token.status_code == 200, token.text
        with db_factory() as db:
            stored = db.query(OAuth2Token).filter_by(access_token=token.json()["access_token"])
            assert stored.one().resource is None

    def test_foreign_resource_is_invalid_target(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        grant = _authorize(api, scope="memory:read").json()
        _approve(api, db_factory, grant)

        token = _poll(api, grant, resource="https://other.example/mcp")

        assert token.status_code == 400
        assert token.json()["error"] == "invalid_target"
        with db_factory() as db:
            assert db.query(OAuth2Token).count() == 0

    def test_foreign_resource_before_approval_is_invalid_target(self, api: TestClient) -> None:
        grant = _authorize(api, scope="memory:read").json()

        token = _poll(api, grant, resource="https://other.example/mcp")

        # Not authorization_pending: the resource is checked first.
        assert token.status_code == 400
        assert token.json()["error"] == "invalid_target"


class TestDeviceConfirmLogging:
    def test_confirm_logs_only_a_user_code_prefix(self, api: TestClient) -> None:
        grant = _authorize(api, scope="memory:read").json()
        user_code = grant["user_code"]

        with capture_logs() as events:
            confirmed = api.post(
                "/api/v1/oauth/device/confirm",
                json={"user_code": user_code, "approve": True},
            )

        assert confirmed.status_code == 200, confirmed.text
        (approved,) = [e for e in events if e["event"] == "device_authorization_approved"]
        assert approved["user_code_prefix"] == user_code[:4]
        assert "user_code" not in approved
        assert not any(user_code in repr(event) for event in events)


class TestDeviceRepeatedResource:
    @pytest.mark.parametrize(
        "resources",
        [["https://other.example/mcp", MCP_RESOURCE], [MCP_RESOURCE, "https://other.example/mcp"]],
        ids=["foreign-then-valid", "valid-then-foreign"],
    )
    def test_poll_with_two_resources_is_invalid_target(
        self, api: TestClient, db_factory: sessionmaker, resources: list[str]
    ) -> None:
        grant = _authorize(api, scope="memory:read").json()
        _approve(api, db_factory, grant)
        pairs = [
            ("grant_type", DEVICE_GRANT),
            ("device_code", grant["device_code"]),
            ("client_id", CLI_CLIENT),
        ] + [("resource", resource) for resource in resources]

        token = api.post(
            "/api/v1/oauth/token",
            content=urlencode(pairs),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert token.status_code == 400
        assert token.json()["error"] == "invalid_target"
        with db_factory() as db:
            assert db.query(OAuth2Token).count() == 0
