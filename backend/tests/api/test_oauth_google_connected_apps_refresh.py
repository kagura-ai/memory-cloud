"""Refresh without rotation for Google connected apps clients (#2773).

Google's connected apps program asks for "persistent linking": a refresh
answers with the exact refresh token the client sent. Only a client DCR
stored as ``google_connected_apps`` whose every redirect URI is in
``GOOGLE_CONNECTED_APPS_REDIRECT_URIS`` gets that; every other client keeps
rotation (RFC 6819 §5.2.2.3). Revocation still ends the link at once.

Drives ``POST /token`` and ``POST /revoke`` through the real Authlib server
against an in-memory SQLite database holding the OAuth tables (the pattern
of ``test_oauth_authorization_code_flow.py``). The row locks and the
concurrent double refresh are covered on PostgreSQL in
``tests/integration/test_oauth_google_connected_apps_db.py``.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from auth.google_connected_apps import keeps_refresh_token  # noqa: E402
from auth.mcp_scopes import DCR_DEFAULT_SCOPE  # noqa: E402
from auth.oauth_token_storage import hash_oauth_token, token_lookup_values  # noqa: E402
from config.settings import get_settings  # noqa: E402
from models.auth import (  # noqa: E402
    OAuth2Client,
    OAuth2Token,
    OAuthTokenStorage,
    User,
)
from utils.datetime import utcnow  # noqa: E402

FRONTEND = "https://memory.example.test"
MCP_RESOURCE = f"{FRONTEND}/mcp"
APP_URI = "https://oauth-redirect.googleusercontent.com/r/example-connected-app"
GEMINI_URI = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-abc"
USER_ID = "gca-user"
GOOGLE_CLIENT = "oauth_gca_google"
GEMINI_CLIENT = "oauth_gca_gemini"
CLAUDE_CLIENT = "oauth_gca_claude"


@pytest.fixture
def db_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (User, OAuth2Client, OAuth2Token, OAuthTokenStorage):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    common: dict[str, Any] = {
        "client_secret_hash": "",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": DCR_DEFAULT_SCOPE,
        "token_endpoint_auth_method": "none",
    }
    with factory() as db:
        db.add(OAuthTokenStorage(id=1, mode="hashed"))
        db.add(
            User(
                user_id=USER_ID,
                email="gca-user@example.test",
                name="GCA User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
        )
        db.add_all(
            [
                OAuth2Client(
                    client_id=GOOGLE_CLIENT,
                    client_name="Google",
                    redirect_uris=[APP_URI],
                    provider="google_connected_apps",
                    **common,
                ),
                OAuth2Client(
                    client_id=GEMINI_CLIENT,
                    client_name="Gemini",
                    redirect_uris=[GEMINI_URI],
                    provider="gemini",
                    **common,
                ),
                OAuth2Client(
                    client_id=CLAUDE_CLIENT,
                    client_name="Claude",
                    redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
                    provider="claude",
                    **common,
                ),
            ]
        )
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "google_connected_apps_redirect_uris", APP_URI)


@pytest.fixture
def api(db_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FRONTEND_URL", FRONTEND)
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)
    with patch("api.routes.oauth.get_sync_session", side_effect=db_factory):
        # Authlib refuses the OAuth endpoints over plain http.
        yield TestClient(app, base_url="https://testserver")


def _issue(db_factory: sessionmaker, client_id: str, **row: Any) -> dict[str, str]:
    """Store a token pair for ``client_id`` as the code grant would (hashed)."""
    tokens = {"access_token": secrets.token_urlsafe(32), "refresh_token": secrets.token_urlsafe(32)}
    with db_factory() as db:
        db.add(
            OAuth2Token(
                client_id=client_id,
                user_id=USER_ID,
                token_type="Bearer",
                access_token=hash_oauth_token(tokens["access_token"]),
                refresh_token=hash_oauth_token(tokens["refresh_token"]),
                scope="memory:read memory:write",
                expires_in=3600,
                resource=MCP_RESOURCE,
                revoked=False,
                **row,
            )
        )
        db.commit()
    return tokens


def _refresh(api: TestClient, client_id: str, refresh_token: str, **extra: str) -> Any:
    return api.post(
        "/api/v1/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            **extra,
        },
    )


def _revoke(api: TestClient, client_id: str, token: str) -> Any:
    return api.post("/api/v1/oauth/revoke", data={"token": token, "client_id": client_id})


def _rows(db_factory: sessionmaker, client_id: str) -> list[OAuth2Token]:
    with db_factory() as db:
        return db.query(OAuth2Token).filter_by(client_id=client_id).order_by(OAuth2Token.id).all()


def _by_access(db_factory: sessionmaker, access_token: str) -> OAuth2Token | None:
    with db_factory() as db:
        return (
            db.query(OAuth2Token)
            .filter(OAuth2Token.access_token.in_(token_lookup_values(access_token)))
            .one_or_none()
        )


def _active(db_factory: sessionmaker, access_token: str) -> bool:
    """What the bearer check (``find_active_oauth_token``) would answer."""
    row = _by_access(db_factory, access_token)
    return row is not None and not row.is_revoked() and not row.is_expired()


class TestPredicate:
    def _client(self, **fields: Any) -> OAuth2Client:
        values: dict[str, Any] = {
            "provider": "google_connected_apps",
            "redirect_uris": [APP_URI],
            "grant_types": ["authorization_code", "refresh_token"],
        }
        values.update(fields)
        return OAuth2Client(**values)

    @pytest.mark.usefixtures("listed")
    def test_listed_google_client_keeps(self) -> None:
        assert keeps_refresh_token(self._client())

    def test_emptied_setting_restores_rotation(self) -> None:
        assert not keeps_refresh_token(self._client())

    @pytest.mark.usefixtures("listed")
    @pytest.mark.parametrize(
        "fields",
        [
            {"redirect_uris": []},
            {"redirect_uris": None},
            {"redirect_uris": [APP_URI, "https://example.com/cb"]},
            {"redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/another-app"]},
            {"provider": "gemini"},
            {"provider": "custom"},
            # A device-code poller gets the tokens without Google's redirector.
            {"grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"]},
            {"grant_types": ["authorization_code", "refresh_token", "client_credentials"]},
        ],
    )
    def test_anything_else_rotates(self, fields: dict[str, Any]) -> None:
        assert not keeps_refresh_token(self._client(**fields))


@pytest.mark.usefixtures("listed")
class TestKeptRefreshToken:
    def test_refresh_returns_the_same_refresh_token_and_a_new_access_token(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["refresh_token"] == tokens["refresh_token"]
        assert body["access_token"] != tokens["access_token"]
        assert body["expires_in"] == 3600
        assert _active(db_factory, body["access_token"])
        # The previous access token was replaced in the row: it no longer exists.
        assert _by_access(db_factory, tokens["access_token"]) is None
        rows = _rows(db_factory, GOOGLE_CLIENT)
        assert len(rows) == 1
        row = rows[0]
        assert row.refresh_token == hash_oauth_token(tokens["refresh_token"])
        assert row.access_token == hash_oauth_token(body["access_token"])
        assert row.refresh_token_revoked_at is None
        assert row.access_token_revoked_at is None
        assert row.resource == MCP_RESOURCE

    def test_the_kept_refresh_token_works_again_and_again(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        accesses = []
        for _ in range(3):
            response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])
            assert response.status_code == 200, response.text
            assert response.json()["refresh_token"] == tokens["refresh_token"]
            accesses.append(response.json()["access_token"])

        assert len(set(accesses)) == 3
        assert [_active(db_factory, a) for a in accesses] == [False, False, True]
        assert len(_rows(db_factory, GOOGLE_CLIENT)) == 1

    def test_refresh_moves_issued_at_forward(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # The access token has expired; the refresh token has no expiry.
        tokens = _issue(db_factory, GOOGLE_CLIENT, issued_at=utcnow() - timedelta(hours=5))
        assert not _active(db_factory, tokens["access_token"])

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        row = _rows(db_factory, GOOGLE_CLIENT)[0]
        assert row.issued_at > utcnow() - timedelta(minutes=5)
        assert _active(db_factory, response.json()["access_token"])

    def test_refresh_after_the_access_token_was_revoked_issues_a_working_one(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        with db_factory() as db:
            db.query(OAuth2Token).update({OAuth2Token.access_token_revoked_at: utcnow()})
            db.commit()

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        assert _active(db_factory, response.json()["access_token"])

    def test_scope_cannot_widen(self, api: TestClient, db_factory: sessionmaker) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        with db_factory() as db:
            db.query(OAuth2Token).update({OAuth2Token.scope: "memory:read"})
            db.commit()

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"], scope="memory:write")

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_scope"

    def test_scope_can_narrow(self, api: TestClient, db_factory: sessionmaker) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"], scope="memory:read")

        assert response.status_code == 200, response.text
        assert response.json()["refresh_token"] == tokens["refresh_token"]
        assert _rows(db_factory, GOOGLE_CLIENT)[0].scope == "memory:read"

    def test_foreign_resource_is_invalid_target(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(
            api, GOOGLE_CLIENT, tokens["refresh_token"], resource="https://other.example/mcp"
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"
        assert _active(db_factory, tokens["access_token"])

    def test_another_client_cannot_use_it(self, api: TestClient, db_factory: sessionmaker) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(api, CLAUDE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    @pytest.mark.parametrize(
        "row",
        [
            {"refresh_token_revoked_at": utcnow()},
            {"revoked": True},
        ],
    )
    def test_a_revoked_kept_token_is_invalid_grant(
        self, api: TestClient, db_factory: sessionmaker, row: dict[str, Any]
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        with db_factory() as db:
            db.query(OAuth2Token).update(row)
            db.commit()

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        stored = _rows(db_factory, GOOGLE_CLIENT)[0]
        assert stored.access_token == hash_oauth_token(tokens["access_token"])

    def test_logs_a_distinct_event_without_token_material(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        with capture_logs() as logs:
            response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        events = [entry for entry in logs if entry["event"] == "oauth_refresh_token_kept"]
        assert len(events) == 1
        assert events[0]["client_id"] == GOOGLE_CLIENT
        assert events[0]["user_id"] == USER_ID
        assert not [entry for entry in logs if entry["event"] == "oauth_refresh_token_rotated"]
        text = repr(logs)
        for value in (tokens["refresh_token"], tokens["access_token"]):
            assert value not in text
        assert response.json()["access_token"] not in text


class TestRotationElsewhere:
    @pytest.mark.usefixtures("listed")
    @pytest.mark.parametrize("client_id", [GEMINI_CLIENT, CLAUDE_CLIENT])
    def test_other_clients_still_rotate(
        self, api: TestClient, db_factory: sessionmaker, client_id: str
    ) -> None:
        tokens = _issue(db_factory, client_id)

        with capture_logs() as logs:
            first = _refresh(api, client_id, tokens["refresh_token"])
        second = _refresh(api, client_id, tokens["refresh_token"])

        assert first.status_code == 200, first.text
        assert first.json()["refresh_token"] != tokens["refresh_token"]
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"
        rows = _rows(db_factory, client_id)
        assert len(rows) == 2
        assert rows[0].refresh_token_revoked_at is not None
        assert rows[0].access_token_revoked_at is not None
        assert [e["event"] for e in logs if e["event"].startswith("oauth_refresh_token_")] == [
            "oauth_refresh_token_rotated"
        ]

    @pytest.mark.parametrize("client_id", [GEMINI_CLIENT, CLAUDE_CLIENT])
    def test_a_revoked_row_cannot_be_refreshed(
        self, api: TestClient, db_factory: sessionmaker, client_id: str
    ) -> None:
        # ``revoked`` set with ``refresh_token_revoked_at`` still NULL.
        tokens = _issue(db_factory, client_id)
        with db_factory() as db:
            db.query(OAuth2Token).update({"revoked": True})
            db.commit()

        response = _refresh(api, client_id, tokens["refresh_token"])

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert len(_rows(db_factory, client_id)) == 1

    @pytest.mark.usefixtures("listed")
    def test_a_google_client_with_the_device_grant_rotates(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        with db_factory() as db:
            client = db.query(OAuth2Client).filter_by(client_id=GOOGLE_CLIENT).one()
            client.grant_types = [
                "urn:ietf:params:oauth:grant-type:device_code",
                "refresh_token",
            ]
            db.commit()
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        assert response.json()["refresh_token"] != tokens["refresh_token"]
        assert _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 400

    def test_emptied_setting_restores_rotation(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        assert response.json()["refresh_token"] != tokens["refresh_token"]
        assert _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 400

    @pytest.mark.usefixtures("listed")
    def test_changed_redirect_uris_restore_rotation(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        with db_factory() as db:
            client = db.query(OAuth2Client).filter_by(client_id=GOOGLE_CLIENT).one()
            client.redirect_uris = [APP_URI, "https://example.com/cb"]
            db.commit()

        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])

        assert response.status_code == 200, response.text
        assert response.json()["refresh_token"] != tokens["refresh_token"]


@pytest.mark.usefixtures("listed")
class TestRevocationEndsTheLink:
    def test_revoking_the_refresh_token(self, api: TestClient, db_factory: sessionmaker) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        refreshed = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).json()

        assert _revoke(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 200

        assert not _active(db_factory, refreshed["access_token"])
        response = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"])
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    def test_revoking_the_current_access_token_ends_the_link(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # Gate1 (#2773): a kept refresh token would otherwise mint a new
        # access token right after the client revoked its access token.
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        assert _revoke(api, GOOGLE_CLIENT, tokens["access_token"]).status_code == 200

        row = _rows(db_factory, GOOGLE_CLIENT)[0]
        assert row.access_token_revoked_at is not None
        assert row.refresh_token_revoked_at is not None
        assert _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 400

    def test_revoking_a_replaced_access_token_is_a_no_op(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # The replaced access token matches no row any more: 200 (RFC 7009
        # §2.2, as for an unknown token) and the link stays.
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        refreshed = _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).json()

        assert _revoke(api, GOOGLE_CLIENT, tokens["access_token"]).status_code == 200

        assert _active(db_factory, refreshed["access_token"])
        assert _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 200

    def test_another_client_cannot_revoke_it(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        tokens = _issue(db_factory, GOOGLE_CLIENT)

        assert _revoke(api, CLAUDE_CLIENT, tokens["access_token"]).status_code == 200

        row = _rows(db_factory, GOOGLE_CLIENT)[0]
        assert row.access_token_revoked_at is None
        assert row.refresh_token_revoked_at is None

    def test_access_token_revocation_of_a_rotating_client_keeps_its_refresh_token(
        self, api: TestClient, db_factory: sessionmaker
    ) -> None:
        # Unchanged for every other client: only the access token is revoked.
        tokens = _issue(db_factory, CLAUDE_CLIENT)

        assert _revoke(api, CLAUDE_CLIENT, tokens["access_token"]).status_code == 200

        row = _rows(db_factory, CLAUDE_CLIENT)[0]
        assert row.access_token_revoked_at is not None
        assert row.refresh_token_revoked_at is None
        assert _refresh(api, CLAUDE_CLIENT, tokens["refresh_token"]).status_code == 200

    def test_bulk_revocation_ends_the_link(self, api: TestClient, db_factory: sessionmaker) -> None:
        # What password reset and the CLI stamp (services/oauth_grant_revocation.py).
        tokens = _issue(db_factory, GOOGLE_CLIENT)
        now = utcnow()
        with db_factory() as db:
            db.query(OAuth2Token).update(
                {
                    OAuth2Token.revoked: True,
                    OAuth2Token.access_token_revoked_at: now,
                    OAuth2Token.refresh_token_revoked_at: now,
                }
            )
            db.commit()

        assert _refresh(api, GOOGLE_CLIENT, tokens["refresh_token"]).status_code == 400
