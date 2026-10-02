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
    link_identity,
    list_identity_links,
    unlink_identity,
)
from services.identity_link_service import LinkedIdentity
from services.security_notification_service import SecurityEvent
from utils.exceptions import NotFoundException

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
    instance.unlink = AsyncMock()
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
