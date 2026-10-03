"""#1784: the identity-link endpoints — the proof is the browser session.

The service is covered in ``tests/integration/test_identity_links_db.py``.
These pin the route layer: an account can be linked only when this session
holds it, and an id it does not hold answers 404 whatever exists.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes import me_account
from api.routes.me_account import (
    IdentityLinkTarget,
    leave_identity_links,
    link_identity,
    list_identity_links,
    unlink_identity,
)
from services.identity_link_service import LinkedIdentity
from services.security_notification_service import SecurityEvent
from utils.exceptions import IdentityLinkSignInRequiredError, NotFoundException

ME = "local:admin"
OTHER = "google-oauth2|123"
SESSION = "session-abc"


def _request(cookie: str | None = SESSION) -> MagicMock:
    request = MagicMock()
    request.cookies = {me_account.auth_module.SESSION_COOKIE_NAME: cookie} if cookie else {}
    request.client = SimpleNamespace(host="203.0.113.7")
    request.headers = {"user-agent": "pytest"}
    return request


@pytest.fixture
def session_manager():
    manager = MagicMock()
    with patch.object(me_account.auth_module, "_session_manager", manager):
        yield manager


@pytest.fixture
def notices():
    with patch.object(me_account, "schedule_security_notification") as schedule:
        yield schedule


@pytest.fixture
def service():
    instance = MagicMock()
    instance.link = AsyncMock(return_value=True)
    instance.unlink = AsyncMock(return_value=frozenset({ME}))
    instance.leave = AsyncMock(return_value=frozenset())
    instance.list_linked = AsyncMock(return_value=[])
    with patch.object(me_account, "IdentityLinkService", return_value=instance):
        yield instance


class TestLinkIdentity:
    @pytest.mark.asyncio
    async def test_links_an_account_signed_in_on_this_session(
        self, session_manager, service, notices
    ):
        session_manager.session_holds_user.return_value = True

        result = await link_identity(
            IdentityLinkTarget(user_id=OTHER), _request(), MagicMock(), {"user_id": ME}, AsyncMock()
        )

        assert result.status == "ok"
        session_manager.session_holds_user.assert_called_once_with(SESSION, OTHER)
        service.link.assert_awaited_once_with(
            ME, OTHER, ip_address="203.0.113.7", user_agent="pytest"
        )
        # Both accounts are told.
        assert [
            (call.kwargs["user_id"], call.kwargs["event"]) for call in notices.call_args_list
        ] == [(ME, SecurityEvent.ACCOUNT_LINKED), (OTHER, SecurityEvent.ACCOUNT_LINKED)]

    @pytest.mark.asyncio
    async def test_an_account_not_in_the_session_is_not_found(
        self, session_manager, service, notices
    ):
        """Naming an id is not proof — and the answer does not say whether
        such an account exists."""
        session_manager.session_holds_user.return_value = False

        with pytest.raises(NotFoundException):
            await link_identity(
                IdentityLinkTarget(user_id=OTHER),
                _request(),
                MagicMock(),
                {"user_id": ME},
                AsyncMock(),
            )

        service.link.assert_not_awaited()
        notices.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_cookie_is_unauthenticated(self, session_manager, service):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await link_identity(
                IdentityLinkTarget(user_id=OTHER),
                _request(cookie=None),
                MagicMock(),
                {"user_id": ME},
                AsyncMock(),
            )

        assert exc.value.status_code == 401
        service.link.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repeating_an_existing_link_sends_no_second_notice(
        self, session_manager, service, notices
    ):
        session_manager.session_holds_user.return_value = True
        service.link.return_value = False

        result = await link_identity(
            IdentityLinkTarget(user_id=OTHER),
            _request(),
            MagicMock(),
            {"user_id": ME},
            AsyncMock(),
        )

        assert result.status == "ok"
        notices.assert_not_called()


class TestFreshSignIn:
    """#1803: holding both accounts is not enough — both must have signed in
    on this session within ``IDENTITY_LINK_SIGN_IN_WINDOW``."""

    @staticmethod
    def _fresh(*accounts: str):
        return lambda _session, account, _window: account in accounts

    @pytest.mark.asyncio
    async def test_both_accounts_are_checked_against_the_window(
        self, session_manager, service, notices
    ):
        session_manager.session_holds_user.return_value = True
        session_manager.proven_within.side_effect = self._fresh(ME, OTHER)

        await link_identity(
            IdentityLinkTarget(user_id=OTHER), _request(), MagicMock(), {"user_id": ME}, AsyncMock()
        )

        checked = {
            (call.args[1], call.args[2]) for call in session_manager.proven_within.call_args_list
        }
        assert checked == {
            (ME, me_account.IDENTITY_LINK_SIGN_IN_WINDOW),
            (OTHER, me_account.IDENTITY_LINK_SIGN_IN_WINDOW),
        }
        service.link.assert_awaited_once()

    def test_the_window_is_ten_minutes(self):
        from datetime import timedelta

        assert me_account.IDENTITY_LINK_SIGN_IN_WINDOW == timedelta(minutes=10)

    @pytest.mark.parametrize("fresh", [(ME,), (OTHER,), ()])
    @pytest.mark.asyncio
    async def test_a_stale_sign_in_on_either_side_refuses_the_link(
        self, session_manager, service, notices, fresh
    ):
        """A link is symmetric: a stale caller would let whoever holds an old
        session hand its private contexts to an account they control, and a
        stale target the other way round."""
        session_manager.session_holds_user.return_value = True
        session_manager.proven_within.side_effect = self._fresh(*fresh)

        with pytest.raises(IdentityLinkSignInRequiredError) as exc:
            await link_identity(
                IdentityLinkTarget(user_id=OTHER),
                _request(),
                MagicMock(),
                {"user_id": ME},
                AsyncMock(),
            )

        assert exc.value.status_code == 403
        assert exc.value.error_code == "AUTH-305"
        service.link.assert_not_awaited()
        notices.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_account_not_in_the_session_is_not_found_before_freshness(
        self, session_manager, service, notices
    ):
        """404 first: a stale-sign-in answer for an id the session does not
        hold would say that such an account exists."""
        session_manager.session_holds_user.return_value = False
        session_manager.proven_within.return_value = False

        with pytest.raises(NotFoundException):
            await link_identity(
                IdentityLinkTarget(user_id=OTHER),
                _request(),
                MagicMock(),
                {"user_id": ME},
                AsyncMock(),
            )

        session_manager.proven_within.assert_not_called()


class TestUnlinkIdentity:
    @pytest.mark.asyncio
    async def test_unlink_does_not_need_the_other_account_signed_in(
        self, session_manager, service, notices
    ):
        result = await unlink_identity(
            IdentityLinkTarget(user_id=OTHER), _request(), MagicMock(), {"user_id": ME}, AsyncMock()
        )

        assert result.status == "ok"
        session_manager.session_holds_user.assert_not_called()
        service.unlink.assert_awaited_once_with(
            ME, OTHER, ip_address="203.0.113.7", user_agent="pytest"
        )
        assert [
            (call.kwargs["user_id"], call.kwargs["event"]) for call in notices.call_args_list
        ] == [(ME, SecurityEvent.ACCOUNT_UNLINKED), (OTHER, SecurityEvent.ACCOUNT_UNLINKED)]


class TestListIdentityLinks:
    @pytest.mark.asyncio
    async def test_lists_linked_accounts_and_the_ones_this_session_could_add(
        self, session_manager, service
    ):
        service.list_linked.return_value = [
            LinkedIdentity(
                user_id=OTHER,
                email="me@example.com",
                name="Me",
                linked_at=datetime(2026, 10, 2, 3, 0, 0),
            )
        ]
        session_manager.list_accounts.return_value = [
            {"user_id": ME, "email": "admin@local", "is_active": True},
            {"user_id": OTHER, "email": "me@example.com"},
            {"sub": "github|9", "email": "gh@example.com", "name": "GH"},
        ]

        result = await list_identity_links(_request(), {"user_id": ME}, AsyncMock())

        assert [(item.user_id, item.linked_at) for item in result.linked] == [
            (OTHER, "2026-10-02T03:00:00Z")
        ]
        # Not the caller, not what is already linked.
        assert [(item.user_id, item.name) for item in result.linkable] == [("github|9", "GH")]

    @pytest.mark.asyncio
    async def test_reports_which_accounts_signed_in_recently(self, session_manager, service):
        """#1803: the page can say which account to sign in to again."""
        session_manager.list_accounts.return_value = [
            {"user_id": ME, "is_active": True},
            {"user_id": "github|9"},
            {"user_id": "google|5"},
        ]
        session_manager.proven_within.side_effect = lambda _session, account, window: (
            account == "github|9" and window == me_account.IDENTITY_LINK_SIGN_IN_WINDOW
        )

        result = await list_identity_links(_request(), {"user_id": ME}, AsyncMock())

        assert result.signed_in_recently is False
        assert [(item.user_id, item.signed_in_recently) for item in result.linkable] == [
            ("github|9", True),
            ("google|5", False),
        ]
        assert result.sign_in_window_minutes == 10

    @pytest.mark.asyncio
    async def test_reports_how_each_account_can_sign_in(self, session_manager, service):
        """#1833: the page offers "Confirm with Google" only to Google accounts."""
        session_manager.list_accounts.return_value = [
            {"user_id": ME, "is_active": True},
            {"user_id": "github|9"},
            {"user_id": "google|5"},
        ]
        db = AsyncMock()
        db.execute.side_effect = [
            [("github|9", "github"), ("google|5", "google")],  # user_oauth_providers
            [  # users: auth_provider, password_hash
                (ME, None, "$argon2..."),
                ("github|9", "github", None),
                ("google|5", None, None),
            ],
        ]

        result = await list_identity_links(_request(), {"user_id": ME}, db)

        assert result.providers == ["password"]
        assert [(item.user_id, item.providers) for item in result.linkable] == [
            ("github|9", ["github"]),
            ("google|5", ["google"]),
        ]
        # One query per table, whatever the number of accounts.
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_an_account_nothing_is_known_about_gets_no_providers(
        self, session_manager, service
    ):
        session_manager.list_accounts.return_value = [
            {"user_id": ME, "is_active": True},
            {"user_id": "google|5"},
        ]

        result = await list_identity_links(_request(), {"user_id": ME}, AsyncMock())

        assert result.providers == []
        assert result.linkable[0].providers == []


class TestSessionOnly:
    """A leaked API key or OAuth token must never be enough to link accounts."""

    def test_the_endpoints_take_the_session_only_dependency(self):
        import inspect

        from auth.dependencies import SessionUser

        for route in (link_identity, unlink_identity, list_identity_links):
            assert inspect.signature(route).parameters["user"].annotation in (
                SessionUser,
                "SessionUser",
            )


class TestUnlinkInALargerSet:
    """#1807: unlinking one account also separates it from the other
    remaining accounts, so each of them is told."""

    @pytest.mark.asyncio
    async def test_every_account_the_unlinked_one_leaves_is_notified(self, service, notices):
        service.unlink = AsyncMock(return_value=frozenset({ME, "github|9"}))

        await unlink_identity(
            IdentityLinkTarget(user_id=OTHER), _request(), MagicMock(), {"user_id": ME}, AsyncMock()
        )

        assert [call.kwargs["user_id"] for call in notices.call_args_list] == [
            ME,
            OTHER,
            "github|9",
        ]


class TestLeave:
    """#1807: the session user leaves its set; the others stay linked."""

    @pytest.mark.asyncio
    async def test_leaving_notifies_every_account_of_the_former_set(self, service, notices):
        service.leave = AsyncMock(return_value=frozenset({OTHER, "github|9"}))

        result = await leave_identity_links(_request(), MagicMock(), {"user_id": ME}, AsyncMock())

        assert result.status == "ok"
        service.leave.assert_awaited_once_with(ME, ip_address="203.0.113.7", user_agent="pytest")
        assert [
            (call.kwargs["user_id"], call.kwargs["event"]) for call in notices.call_args_list
        ] == [
            (ME, SecurityEvent.ACCOUNT_UNLINKED),
            ("github|9", SecurityEvent.ACCOUNT_UNLINKED),
            (OTHER, SecurityEvent.ACCOUNT_UNLINKED),
        ]

    @pytest.mark.asyncio
    async def test_an_account_in_no_set_is_not_found(self, service, notices):
        service.leave = AsyncMock(side_effect=NotFoundException("Identity link"))

        with pytest.raises(NotFoundException):
            await leave_identity_links(_request(), MagicMock(), {"user_id": ME}, AsyncMock())

        notices.assert_not_called()


class TestBrowserSessionOnlyOverHTTP:
    """#1807: every identity-link endpoint refuses an API key and an OAuth
    bearer token — through the real router and its ``SessionUser``
    dependency, not by calling the handlers directly."""

    ENDPOINTS = [
        ("GET", "/api/v1/me/account/identity-links", None),
        ("POST", "/api/v1/me/account/identity-links", {"user_id": OTHER}),
        ("POST", "/api/v1/me/account/identity-links/unlink", {"user_id": OTHER}),
        ("POST", "/api/v1/me/account/identity-links/leave", None),
    ]

    @pytest.fixture
    def client(self, service):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from db.base import get_db

        app = FastAPI()
        app.include_router(me_account.router, prefix="/api/v1")

        async def no_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = no_db
        return TestClient(app)

    @pytest.mark.parametrize("method,path,body", ENDPOINTS)
    @pytest.mark.parametrize(
        "token", ["kagura_" + "a" * 40, "oauth-access-token"], ids=["api_key", "bearer"]
    )
    def test_a_bearer_credential_is_refused(self, client, service, method, path, body, token):
        response = client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 403
        service.list_linked.assert_not_awaited()
        service.link.assert_not_awaited()
        service.unlink.assert_not_awaited()
        service.leave.assert_not_awaited()

    @pytest.mark.parametrize("method,path,body", ENDPOINTS)
    def test_no_credential_is_unauthenticated(self, client, service, method, path, body):
        response = client.request(method, path, json=body)

        assert response.status_code == 401
        service.leave.assert_not_awaited()
