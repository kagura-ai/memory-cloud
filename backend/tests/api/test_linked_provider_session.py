"""A sign-in through a linked provider opens a session for the owning account (#1805).

A provider linked to another account (#517) belongs to that account's
``user_id``, not to the IdP ``sub``. Both OAuth callbacks used to key the
session — and the #114 invalidation and the personal-workspace step around it —
by the raw ``sub``, so a user who signed in with a linked secondary provider
landed in a session for an id that owns nothing.

Driven through the whole callback with the IdP exchange stubbed, the same way
``test_auth_terms_acceptance.py`` does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import auth as auth_routes

OWNER_ID = "owner-account-1"
OWNER_EMAIL = "owner@example.test"
GOOGLE_SUB = "108"
GITHUB_SUB = "583231"
IDP_EMAIL = "linked@example.test"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


@pytest.fixture
def manager(monkeypatch) -> MagicMock:
    m = MagicMock()
    m._redis = FakeRedis()
    m._redis.store["oauth2_state:st1"] = "pending"
    m.delete_user_sessions.return_value = 0
    m.create_session.return_value = "sess-1"
    monkeypatch.setattr(auth_routes, "_session_manager", m)
    return m


@pytest.fixture
def signed_in_path(monkeypatch) -> SimpleNamespace:
    """Everything between the IdP exchange and the cookie, stubbed to pass."""
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "_note_provider_sign_in", AsyncMock())
    role = MagicMock(value="user")
    monkeypatch.setattr(
        auth_routes,
        "get_role_manager",
        lambda: SimpleNamespace(ensure_user=AsyncMock(return_value=role)),
    )

    async def _fake_db():
        yield MagicMock()

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)

    invitations = MagicMock()
    invitations.return_value.get_pending_invitations_for_email = AsyncMock(return_value=[])
    monkeypatch.setattr("services.invitation_service.InvitationService", invitations)
    workspaces = MagicMock()
    workspaces.return_value.ensure_personal_workspace = AsyncMock()
    monkeypatch.setattr(auth_routes, "WorkspaceService", workspaces)

    owning = AsyncMock(return_value=(OWNER_ID, OWNER_EMAIL))
    monkeypatch.setattr(auth_routes, "_owning_user", owning)
    return SimpleNamespace(
        ensure_workspace=workspaces.return_value.ensure_personal_workspace, owning=owning
    )


@pytest.fixture
def google_idp(monkeypatch) -> None:
    m = MagicMock()
    m.get_user_info_web.return_value = {
        "sub": GOOGLE_SUB,
        "email": IDP_EMAIL,
        "email_verified": True,
    }
    monkeypatch.setattr(auth_routes, "_oauth2_manager", m)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")


@pytest.fixture
def github_idp(monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
    monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
    monkeypatch.setattr(
        auth_routes,
        "_github_get_user_info",
        AsyncMock(
            return_value={
                "sub": GITHUB_SUB,
                "email": IDP_EMAIL,
                "email_verified": True,
                "login": "octo",
            }
        ),
    )


async def _callback(provider: str):
    handler = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await handler(FakeRequest(), code="c", state="st1", error=None, error_description=None)


PROVIDERS = [("google", GOOGLE_SUB, "google_idp"), ("github", GITHUB_SUB, "github_idp")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_session_belongs_to_the_owning_account(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)

    response = await _callback(provider)

    assert response.status_code == 303
    assert "kagura_session=sess-1" in response.headers["set-cookie"]
    assert signed_in_path.owning.await_args.args[1:] == (provider, sub)
    session_data = manager.create_session.call_args.args[0]
    assert session_data["user_id"] == OWNER_ID
    assert session_data["sub"] == OWNER_ID
    assert session_data["email"] == OWNER_EMAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_old_sessions_of_the_owning_account_are_invalidated(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    # #114 must reach the owner's earlier sessions, not an id that owns none.
    request.getfixturevalue(idp)

    await _callback(provider)

    assert manager.delete_user_sessions.call_args.args[0] == OWNER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_personal_workspace_is_ensured_for_the_owning_account(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)

    await _callback(provider)

    assert signed_in_path.ensure_workspace.await_args.kwargs["user_id"] == OWNER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_refresh_through_a_linked_provider_matches_the_owning_account(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    # POST /me/refresh-oauth pins the SESSION's user_id — the owner's — so the
    # same-user check (#515) must compare against the owner of the identity.
    request.getfixturevalue(idp)
    manager._redis.store["oauth2_state_intent:st1"] = "refresh"
    manager._redis.store["oauth2_state_user:st1"] = OWNER_ID

    response = await _callback(provider)

    assert "refresh_user_mismatch" not in response.headers["location"]
    assert "/profile" in response.headers["location"]
    manager.create_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_refresh_from_another_account_is_still_refused(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)
    manager._redis.store["oauth2_state_intent:st1"] = "refresh"
    manager._redis.store["oauth2_state_user:st1"] = "someone-else"

    response = await _callback(provider)

    assert "refresh_user_mismatch" in response.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_unresolved_owner_falls_back_to_the_sub(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    # No link row and no users row keyed by the sub (lookup failed): keep the
    # former behaviour rather than failing the sign-in.
    request.getfixturevalue(idp)
    signed_in_path.owning.return_value = None

    await _callback(provider)

    session_data = manager.create_session.call_args.args[0]
    assert session_data["user_id"] == sub
    assert session_data["email"] == IDP_EMAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_owner_lookup_failure_fails_the_sign_in(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    # Session identity must not fail open: a DB error after ensure_user would
    # otherwise open a session for an id that may own nothing.
    from sqlalchemy.exc import OperationalError

    request.getfixturevalue(idp)
    signed_in_path.owning.side_effect = OperationalError("select", {}, Exception("down"))

    response = await _callback(provider)

    assert "error=oauth_failed" in response.headers["location"]
    manager.create_session.assert_not_called()
    manager.delete_user_sessions.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_add_account_through_a_linked_provider_adds_the_owner(
    manager, signed_in_path, request, provider, sub, idp, monkeypatch
) -> None:
    # #1488 "add another account": the appended identity is the owner, and the
    # container being added to survives the #114 sweep.
    request.getfixturevalue(idp)
    monkeypatch.setattr(
        auth_routes, "_take_add_account_intent", lambda _state, _req: ("add", "sess-A")
    )
    manager.add_account.return_value = True

    response = await _callback(provider)

    assert "kagura_session=sess-A" in response.headers["set-cookie"]
    assert manager.delete_user_sessions.call_args.args[0] == OWNER_ID
    assert manager.delete_user_sessions.call_args.kwargs["exclude_session_id"] == "sess-A"
    session_id, identity = manager.add_account.call_args.args
    assert session_id == "sess-A"
    assert identity["user_id"] == OWNER_ID
    manager.create_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_invitations_are_looked_up_by_the_owning_account_email(
    manager, signed_in_path, request, provider, sub, idp, monkeypatch
) -> None:
    # #276 skips the personal workspace while invitations are pending; they
    # are addressed to the account, so look them up by the owner's email.
    invitations = MagicMock()
    lookup = AsyncMock(return_value=[])
    invitations.return_value.get_pending_invitations_for_email = lookup
    monkeypatch.setattr("services.invitation_service.InvitationService", invitations)
    request.getfixturevalue(idp)

    await _callback(provider)

    assert lookup.await_args.kwargs["email"] == OWNER_EMAIL


class TestSessionOwnerHelper:
    """``_session_owner`` on its own: resolved owner, no owner row, no DB yielded."""

    @pytest.mark.asyncio
    async def test_returns_the_resolved_owner(self, monkeypatch) -> None:
        async def _fake_db():
            yield MagicMock()

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        monkeypatch.setattr(
            auth_routes, "_owning_user", AsyncMock(return_value=(OWNER_ID, OWNER_EMAIL))
        )

        assert await auth_routes._session_owner("google", GOOGLE_SUB, IDP_EMAIL) == (
            OWNER_ID,
            OWNER_EMAIL,
        )

    @pytest.mark.asyncio
    async def test_no_owner_row_keeps_the_sub(self, monkeypatch) -> None:
        async def _fake_db():
            yield MagicMock()

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        monkeypatch.setattr(auth_routes, "_owning_user", AsyncMock(return_value=None))

        assert await auth_routes._session_owner("github", GITHUB_SUB, IDP_EMAIL) == (
            GITHUB_SUB,
            IDP_EMAIL,
        )

    @pytest.mark.asyncio
    async def test_no_session_yielded_keeps_the_sub(self, monkeypatch) -> None:
        async def _empty_db():
            return
            yield  # pragma: no cover

        monkeypatch.setattr(auth_routes, "get_db", _empty_db)
        owning = AsyncMock()
        monkeypatch.setattr(auth_routes, "_owning_user", owning)

        assert await auth_routes._session_owner("google", GOOGLE_SUB, IDP_EMAIL) == (
            GOOGLE_SUB,
            IDP_EMAIL,
        )
        owning.assert_not_awaited()
