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

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes import auth as auth_routes
from api.routes.auth import SessionOwner
from auth.session import SessionManager
from utils.datetime import utcnow

OWNER_ID = "owner-account-1"
OWNER_EMAIL = "owner@example.test"
OWNER_NAME = "Owner Name"
OWNER_PICTURE = "https://img.example.test/owner.png"
IDP_NAME = "Provider Profile Name"
IDP_PICTURE = "https://img.example.test/provider.png"
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

    # The identity is linked to another account, long enough ago to prove it
    # (#1875: a provider attached inside the link window proves nothing).
    owning = AsyncMock(return_value=linked_owner())
    monkeypatch.setattr(auth_routes, "_owning_user", owning)
    # #1875: no separate account is keyed by the sub unless a test says so.
    account_exists = AsyncMock(return_value=False)
    monkeypatch.setattr(auth_routes, "_account_exists", account_exists)
    return SimpleNamespace(
        ensure_workspace=workspaces.return_value.ensure_personal_workspace,
        owning=owning,
        account_exists=account_exists,
    )


def linked_owner(*, attached: timedelta = timedelta(days=30)) -> SessionOwner:
    """The owner of a linked identity that was attached ``attached`` ago."""
    return SessionOwner(OWNER_ID, OWNER_EMAIL, OWNER_NAME, OWNER_PICTURE, utcnow() - attached)


@pytest.fixture
def google_idp(monkeypatch) -> None:
    m = MagicMock()
    m.get_user_info_web.return_value = {
        "sub": GOOGLE_SUB,
        "email": IDP_EMAIL,
        "email_verified": True,
        "name": IDP_NAME,
        "picture": IDP_PICTURE,
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
                "name": IDP_NAME,
                "picture": IDP_PICTURE,
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

    assert manager.delete_user_sessions.call_args_list[0].args[0] == OWNER_ID


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
    assert manager.delete_user_sessions.call_args_list[0].args[0] == OWNER_ID
    assert all(
        c.kwargs["exclude_session_id"] == "sess-A"
        for c in manager.delete_user_sessions.call_args_list
    )
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
        owner = linked_owner()
        monkeypatch.setattr(auth_routes, "_owning_user", AsyncMock(return_value=owner))

        assert await auth_routes._session_owner("google", GOOGLE_SUB, IDP_EMAIL) == owner

    @pytest.mark.asyncio
    async def test_no_owner_row_keeps_the_sub(self, monkeypatch) -> None:
        async def _fake_db():
            yield MagicMock()

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        monkeypatch.setattr(auth_routes, "_owning_user", AsyncMock(return_value=None))
        warning = MagicMock()
        monkeypatch.setattr(auth_routes.logger, "warning", warning)

        assert await auth_routes._session_owner("github", GITHUB_SUB, IDP_EMAIL) == SessionOwner(
            GITHUB_SUB, IDP_EMAIL
        )
        # #1875: the sub, so the identity that owns nothing can be found.
        warning.assert_called_once_with(
            "session_owner_not_found", provider="github", idp_sub=GITHUB_SUB
        )

    @pytest.mark.asyncio
    async def test_no_session_yielded_keeps_the_sub(self, monkeypatch) -> None:
        async def _empty_db():
            return
            yield  # pragma: no cover

        monkeypatch.setattr(auth_routes, "get_db", _empty_db)
        owning = AsyncMock()
        monkeypatch.setattr(auth_routes, "_owning_user", owning)

        assert await auth_routes._session_owner("google", GOOGLE_SUB, IDP_EMAIL) == SessionOwner(
            GOOGLE_SUB, IDP_EMAIL
        )
        owning.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_sessions_keyed_by_the_sub_before_the_fix_are_invalidated_too(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    # A session opened before #1805 carries the sub as user_id; #114 must not
    # leave it alive beside the owner's new one.
    request.getfixturevalue(idp)

    await _callback(provider)

    invalidated = [c.args[0] for c in manager.delete_user_sessions.call_args_list]
    assert invalidated == [OWNER_ID, sub]


@pytest.mark.asyncio
async def test_primary_provider_sign_in_invalidates_once(manager, signed_in_path, google_idp):
    signed_in_path.owning.return_value = SessionOwner(GOOGLE_SUB, IDP_EMAIL)

    await _callback("google")

    assert [c.args[0] for c in manager.delete_user_sessions.call_args_list] == [GOOGLE_SUB]


# --- #1875: the legacy sub sweep must not reach a live account ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_the_sub_sweep_is_skipped_when_an_account_has_that_id(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)
    signed_in_path.account_exists.return_value = True

    await _callback(provider)

    signed_in_path.account_exists.assert_awaited_once_with(sub)
    assert [c.args[0] for c in manager.delete_user_sessions.call_args_list] == [OWNER_ID]


@pytest.fixture
def real_manager(monkeypatch) -> SessionManager:
    """A real ``SessionManager`` over the fake Redis, so what the callback
    deletes and writes is what a later request reads."""
    from tests.auth.test_session_container import FakeRedis as SessionFakeRedis

    fake = SessionFakeRedis()
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    m = SessionManager(redis_url="redis://fake:6379")
    fake.setex("oauth2_state:st1", 300, "pending")
    monkeypatch.setattr(auth_routes, "_session_manager", m)
    return m


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_a_password_session_of_the_account_keyed_by_the_sub_survives(
    real_manager, signed_in_path, request, provider, sub, idp
) -> None:
    """The account whose ``user_id`` is this sub set a password and unlinked
    the provider, which was then linked to OWNER. Its own session — and the
    other account signed in beside it — must outlive OWNER's sign-ins."""
    request.getfixturevalue(idp)
    signed_in_path.account_exists.return_value = True
    theirs = real_manager.create_session(
        {"sub": sub, "user_id": sub, "email": "former@example.test"}, proven_at=utcnow()
    )
    real_manager.add_account(
        theirs, {"sub": "local:x", "user_id": "local:x", "email": "x@example.test"}
    )

    response = await _callback(provider)

    assert response.status_code == 303
    assert real_manager.session_holds_user(theirs, sub)
    assert {a["user_id"] for a in real_manager.list_accounts(theirs)} == {sub, "local:x"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_a_session_keyed_by_a_sub_that_is_no_account_is_still_swept(
    real_manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)
    legacy = real_manager.create_session({"sub": sub, "user_id": sub, "email": IDP_EMAIL})

    await _callback(provider)

    assert not real_manager.session_holds_user(legacy, sub)


class TestAccountExists:
    @staticmethod
    def _db(monkeypatch, row) -> MagicMock:
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(first=lambda: row))

        async def _fake_db():
            yield db

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        return db

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("row", "expected"), [((GITHUB_SUB,), True), (None, False)])
    async def test_follows_the_users_row(self, monkeypatch, row, expected) -> None:
        db = self._db(monkeypatch, row)

        assert await auth_routes._account_exists(GITHUB_SUB) is expected
        assert GITHUB_SUB in str(db.execute.await_args.args[0].compile().params.values())

    @pytest.mark.asyncio
    async def test_no_database_means_no_account(self, monkeypatch) -> None:
        async def _empty_db():
            return
            yield  # pragma: no cover

        monkeypatch.setattr(auth_routes, "get_db", _empty_db)

        assert await auth_routes._account_exists(GITHUB_SUB) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
    async def test_a_lookup_failure_fails_the_sign_in(
        self, manager, signed_in_path, request, provider, sub, idp
    ) -> None:
        from sqlalchemy.exc import OperationalError

        request.getfixturevalue(idp)
        signed_in_path.account_exists.side_effect = OperationalError("select", {}, Exception("x"))

        response = await _callback(provider)

        assert "error=oauth_failed" in response.headers["location"]
        manager.create_session.assert_not_called()


# --- #1875: the session shows the owning account, not the provider used -------


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_a_linked_sign_in_carries_the_owners_name_and_picture(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)

    await _callback(provider)

    session_data = manager.create_session.call_args.args[0]
    assert session_data["name"] == OWNER_NAME
    assert session_data["picture"] == OWNER_PICTURE


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "sub", "idp"), PROVIDERS)
async def test_the_accounts_own_identity_keeps_the_provider_profile(
    manager, signed_in_path, request, provider, sub, idp
) -> None:
    request.getfixturevalue(idp)
    signed_in_path.owning.return_value = SessionOwner(sub, IDP_EMAIL, "Stored Name", None)

    await _callback(provider)

    session_data = manager.create_session.call_args.args[0]
    assert session_data["name"] == IDP_NAME
    assert session_data["picture"] == IDP_PICTURE
