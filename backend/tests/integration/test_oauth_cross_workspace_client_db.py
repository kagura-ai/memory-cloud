"""A manual OAuth client serves users of every workspace (#2778).

Microsoft 365 Copilot cannot use our DCR (it needs a client secret, and DCR
registers public clients), so its client is a static confidential client
created with ``POST /api/v1/oauth/clients`` and the Teams redirect URI. That
client belongs to one account and workspace (``owner_id`` / ``workspace_id``),
and every Microsoft 365 user who connects authorizes it. This pins, on
PostgreSQL, that user B of another workspace can:

- authorize the client (consent → code → token), the token being B's;
- call tools as B: the MCP bearer check answers B, and a tool sees B's
  workspace, never the client owner's;
- refresh under rotation: the new pair works, the old refresh token and the
  old access token do not, and the new refresh token rotates again.

Every database session is on the test database: the OAuth routes' sync
session and ``db.base.get_db`` are pointed at it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker

from api.routes import oauth as oauth_routes
from auth.consent_csrf import consent_csrf_token
from auth.history_principal import set_history_principal
from auth.oauth2_server import _OAuthUser
from auth.workspace_roles import WorkspaceRole
from config.database import to_sync_database_url
from models.auth import (
    Context,
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2Token,
    User,
    Workspace,
    WorkspaceMember,
)
from models.operation_history import OperationHistory
from tests.conftest import TEST_DATABASE_URL
from utils.exceptions import AuthenticationError

pytestmark = pytest.mark.asyncio(loop_scope="session")

FRONTEND = "https://memory.example.test"
TEAMS_REDIRECT_URI = "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"
SESSION_ID = "sid-cross-workspace"


@pytest.fixture
def sync_factory():
    engine = create_engine(to_sync_database_url(TEST_DATABASE_URL))
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def world(async_engine) -> AsyncIterator[SimpleNamespace]:
    """User A owns workspace WA (and the client); user B owns WB only."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    tag = uuid4().hex[:8]
    a, b = f"xw-a-{tag}", f"xw-b-{tag}"
    wa, wb = uuid4(), uuid4()
    ca, cb = uuid4(), uuid4()
    async with factory() as s:
        users = {
            uid: User(user_id=uid, email=f"{uid}@example.test", name=uid, role="user")
            for uid in (a, b)
        }
        s.add_all(users.values())
        await s.flush()
        for wid, owner in ((wa, a), (wb, b)):
            s.add(
                Workspace(
                    id=wid,
                    name=f"xw-{owner}",
                    plan_name="pro",
                    owner_user_id=owner,
                    daily_api_limit=50000,
                    weekly_api_limit=250000,
                )
            )
        await s.flush()
        # Each user works in their own workspace (what list_contexts reads).
        users[a].current_workspace_id = wa
        users[b].current_workspace_id = wb
        s.add_all(
            [
                WorkspaceMember(workspace_id=wa, user_id=a, role=WorkspaceRole.OWNER),
                WorkspaceMember(workspace_id=wb, user_id=b, role=WorkspaceRole.OWNER),
                Context(id=ca, workspace_id=wa, name=f"a-ctx-{tag}", created_by=a),
                Context(id=cb, workspace_id=wb, name=f"b-ctx-{tag}", created_by=b),
            ]
        )
        await s.commit()
    set_history_principal("session")
    try:
        yield SimpleNamespace(factory=factory, a=a, b=b, wa=wa, wb=wb, ca=ca, cb=cb)
    finally:
        set_history_principal()
        async with factory() as s:
            for stmt in (
                update(User).where(User.user_id.in_([a, b])).values(current_workspace_id=None),
                delete(OAuth2Token).where(OAuth2Token.user_id.in_([a, b])),
                delete(OAuth2AuthorizationCode).where(OAuth2AuthorizationCode.user_id.in_([a, b])),
                delete(OAuth2Client).where(OAuth2Client.owner_id == a),
                delete(OperationHistory).where(OperationHistory.workspace_id.in_([wa, wb])),
                delete(Context).where(Context.id.in_([ca, cb])),
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_([wa, wb])),
                delete(Workspace).where(Workspace.id.in_([wa, wb])),
                delete(User).where(User.user_id.in_([a, b])),
            ):
                await s.execute(stmt)
            await s.commit()


def _request(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.9"),
        headers={"user-agent": "pytest-ua"},
        cookies={},
        query_params={},
        state=SimpleNamespace(user={"user_id": user_id}),
    )


async def _create_client(world, sync_factory, method: str) -> SimpleNamespace:
    """A creates the Microsoft client in WA through the route."""
    import utils.encryption

    with (
        patch.object(oauth_routes, "get_sync_session", sync_factory),
        patch.object(
            utils.encryption,
            "get_encryptor",
            lambda: SimpleNamespace(encrypt=lambda value: "enc"),
        ),
    ):
        created = await oauth_routes.create_oauth2_client(
            _request(world.a),
            BackgroundTasks(),
            oauth_routes.OAuth2ClientCreateRequest(
                client_name="Microsoft 365 Copilot",
                redirect_uris=[TEAMS_REDIRECT_URI],
                token_endpoint_auth_method=method,
            ),
            {"user_id": world.a, "current_workspace_id": world.wa},
        )
    assert created.owner_id == world.a
    assert created.redirect_uris == [TEAMS_REDIRECT_URI]
    assert created.token_endpoint_auth_method == method
    with sync_factory() as db:
        row = db.query(OAuth2Client).filter_by(client_id=created.client_id).one()
        assert str(row.workspace_id) == str(world.wa)
    return SimpleNamespace(id=created.client_id, secret=created.client_secret, method=method)


# The request default, and HTTP Basic for a token store that sends that.
METHODS = pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])


class _Browser:
    """The OAuth endpoints as user B's browser and Microsoft's token store see them."""

    def __init__(self, world, sync_factory, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_URL", FRONTEND)
        monkeypatch.delenv("MCP_BASE_PATH", raising=False)
        self.user = _OAuthUser(
            user_id=world.b, email=f"{world.b}@example.test", session_id=SESSION_ID
        )
        self.patches = [
            patch.object(oauth_routes, "get_sync_session", sync_factory),
            patch.object(oauth_routes, "get_current_user_from_session", return_value=self.user),
            patch("auth.oauth2_server.browser_session_is_live", return_value=True),
            # Neither step reads what this test pins; both would reach the
            # app's own database otherwise.
            patch.object(oauth_routes, "_save_consent_default_context", AsyncMock()),
            patch.object(oauth_routes, "schedule_security_notification"),
        ]

    def __enter__(self) -> TestClient:
        for p in self.patches:
            p.start()
        from api.main import app

        return TestClient(app, base_url="https://testserver")

    def __exit__(self, *exc) -> None:
        for p in reversed(self.patches):
            p.stop()


def _token(api: TestClient, client: SimpleNamespace, form: dict, secret: str | None = None):
    """``POST /token`` authenticated the way ``client`` registered."""
    secret = client.secret if secret is None else secret
    if client.method == "client_secret_basic":
        return api.post("/api/v1/oauth/token", data=form, auth=(client.id, secret))
    return api.post(
        "/api/v1/oauth/token", data={**form, "client_id": client.id, "client_secret": secret}
    )


def _consent_and_exchange(api: TestClient, client: SimpleNamespace) -> dict:
    verifier = secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": client.id,
        "redirect_uri": TEAMS_REDIRECT_URI,
        "state": "copilot-state",
        "scope": "memory:read memory:write",
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    approved = api.post(
        f"/api/v1/oauth/authorize?{urlencode(params)}",
        data={"confirm": "yes", "csrf_token": consent_csrf_token(SESSION_ID)},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text[:300]
    location = approved.headers["location"]
    assert location.startswith(TEAMS_REDIRECT_URI + "?"), location
    query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    assert query["state"] == "copilot-state"
    token = _token(
        api,
        client,
        {
            "grant_type": "authorization_code",
            "code": query["code"],
            "redirect_uri": TEAMS_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()


def _refresh(
    api: TestClient, client: SimpleNamespace, refresh_token: str, secret: str | None = None
):
    return _token(
        api, client, {"grant_type": "refresh_token", "refresh_token": refresh_token}, secret
    )


@pytest.fixture
def test_db(world):
    """``db.base.get_db`` on the test database, as the MCP auth and tools read it."""

    async def get_db() -> AsyncIterator[AsyncSession]:
        async with world.factory() as session:
            yield session

    with patch("db.base.get_db", get_db):
        yield


async def _mcp_user(access_token: str) -> tuple:
    from mcp_server.auth import authenticate_mcp_request

    return await authenticate_mcp_request(f"Bearer {access_token}")


async def _listed_contexts(user_id: str) -> set[str]:
    from mcp_server.tools import execute_tool_call

    with patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))):
        result = await execute_tool_call("list_contexts", {}, user_id, None)
    payload = json.loads(result[0].text)
    assert payload["status"] == "success", payload
    return {item["id"] for item in payload["contexts"]}


@METHODS
async def test_a_user_of_another_workspace_connects_calls_and_refreshes(
    world, sync_factory, test_db, monkeypatch, method
):
    client = await _create_client(world, sync_factory, method)

    with _Browser(world, sync_factory, monkeypatch) as api:
        tokens = await asyncio.to_thread(_consent_and_exchange, api, client)

        # The token is B's: the client's owner and workspace play no part.
        assert await _mcp_user(tokens["access_token"]) == (world.b, None, None)
        listed = await _listed_contexts(world.b)
        assert str(world.cb) in listed
        assert str(world.ca) not in listed

        # Refresh rotates: a new pair that works, the old one ends.
        refreshed = await asyncio.to_thread(_refresh, api, client, tokens["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        rotated = refreshed.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]
        assert rotated["access_token"] != tokens["access_token"]
        assert await _mcp_user(rotated["access_token"]) == (world.b, None, None)
        with pytest.raises(AuthenticationError):
            await _mcp_user(tokens["access_token"])

        replayed = await asyncio.to_thread(_refresh, api, client, tokens["refresh_token"])
        assert replayed.status_code == 400
        assert replayed.json()["error"] == "invalid_grant"

        again = await asyncio.to_thread(_refresh, api, client, rotated["refresh_token"])
        assert again.status_code == 200, again.text
        assert await _mcp_user(again.json()["access_token"]) == (world.b, None, None)


@METHODS
async def test_the_refresh_needs_the_client_secret(
    world, sync_factory, test_db, monkeypatch, method
):
    """A confidential client: a refresh without its secret is refused."""
    client = await _create_client(world, sync_factory, method)

    with _Browser(world, sync_factory, monkeypatch) as api:
        tokens = await asyncio.to_thread(_consent_and_exchange, api, client)
        refused = await asyncio.to_thread(
            _refresh, api, client, tokens["refresh_token"], "not-the-secret"
        )
        assert refused.status_code in (400, 401)
        assert refused.json()["error"] == "invalid_client"
        # The refused attempt did not use up the refresh token.
        kept = await asyncio.to_thread(_refresh, api, client, tokens["refresh_token"])
        assert kept.status_code == 200, kept.text


async def test_the_registered_method_is_the_only_one_accepted(
    world, sync_factory, test_db, monkeypatch
):
    """A client created for the body form refuses HTTP Basic: the client's method
    has to be the one Microsoft sends. The owner switches it in place (the
    documented remedy), keeping the client ID and secret, and then the body form
    is the one refused."""
    post = await _create_client(world, sync_factory, "client_secret_post")

    with _Browser(world, sync_factory, monkeypatch) as api:
        tokens = await asyncio.to_thread(_consent_and_exchange, api, post)
        as_basic = SimpleNamespace(id=post.id, secret=post.secret, method="client_secret_basic")
        refused = await asyncio.to_thread(_refresh, api, as_basic, tokens["refresh_token"])
        assert refused.status_code in (400, 401)
        assert refused.json()["error"] == "invalid_client"

        updated = await oauth_routes.update_oauth2_client(
            _request(world.a),
            post.id,
            oauth_routes.OAuth2ClientUpdateRequest(
                token_endpoint_auth_method="client_secret_basic"
            ),
            {"user_id": world.a},
        )
        assert updated.token_endpoint_auth_method == "client_secret_basic"

        switched = await asyncio.to_thread(_refresh, api, as_basic, tokens["refresh_token"])
        assert switched.status_code == 200, switched.text
        as_post = await asyncio.to_thread(_refresh, api, post, switched.json()["refresh_token"])
        assert as_post.status_code in (400, 401)
        assert as_post.json()["error"] == "invalid_client"
