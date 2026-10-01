"""Security notices scheduled by the credential and OAuth routes (Issue #1752).

Each route is called directly with its collaborators mocked; the assertions
are on the ``BackgroundTasks`` it leaves behind. The password and unlink
routes are covered next to their other tests (``test_password_routes.py``,
``test_account_linking.py``). The first-time-client check and the actor label
run against real Postgres at the bottom.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes import api_keys as api_keys_routes
from api.routes import member_credentials as mc
from api.routes import oauth as oauth_routes
from models.auth import (
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
    User,
    UserOAuthProvider,
)
from services import security_notification_service as sns
from utils.datetime import utcnow

PLAINTEXT_KEY = "kagura_" + "Zm9vYmFyYmF6cXV4" * 3
KEY_PREFIX = PLAINTEXT_KEY[:16]
WS = uuid4()


def _request(ip: str = "203.0.113.9") -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        headers={"user-agent": "pytest-ua"},
        cookies={},
        query_params={},
        state=SimpleNamespace(),
    )


@pytest.fixture
def encryptor(monkeypatch) -> None:
    """The secret routes encrypt the new secret at rest; no key in unit tests."""
    import utils.encryption

    monkeypatch.setattr(
        utils.encryption, "get_encryptor", lambda: MagicMock(encrypt=lambda value: "enc")
    )


def _notices(tasks: BackgroundTasks) -> list[tuple[tuple, dict]]:
    return [
        (task.args, dict(task.kwargs))
        for task in tasks.tasks
        if task.func is sns.notify_security_event
    ]


def _new_key(name: str = "deploy") -> SimpleNamespace:
    return SimpleNamespace(
        id=8,
        name=name,
        key_prefix=KEY_PREFIX,
        user_id="owner-1",
        workspace_id=WS,
        created_at=utcnow(),
        last_used_at=None,
        revoked_at=None,
        expires_at=None,
        visibility_expires_at=utcnow(),
        bound_context_id=None,
    )


# ---------------------------------------------------------------------------
# /config/api-keys
# ---------------------------------------------------------------------------


class TestApiKeyRoutes:
    @pytest.mark.asyncio
    async def test_create_schedules_after_commit_and_email_carries_no_key(
        self, monkeypatch
    ) -> None:
        manager = MagicMock()
        manager.db.commit = AsyncMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key()))
        tasks = BackgroundTasks()

        response = await api_keys_routes.create_api_key(
            api_keys_routes.APIKeyCreate(name="deploy"),
            _request(),
            tasks,
            {"user_id": "owner-1"},
            manager=manager,
        )

        assert response.api_key == PLAINTEXT_KEY  # the API still answers with it
        manager.db.commit.assert_awaited_once()
        ((args, kwargs),) = _notices(tasks)
        assert args == ("owner-1", "api_key_created")
        assert kwargs["key_name"] == "deploy"
        assert PLAINTEXT_KEY not in repr(kwargs) and KEY_PREFIX not in repr(kwargs)

        # Run the scheduled notice through the Resend body builder.
        body = await _run_and_capture(monkeypatch, tasks)
        assert '"deploy"' in body
        assert PLAINTEXT_KEY not in body
        assert KEY_PREFIX not in body

    @pytest.mark.asyncio
    async def test_failed_create_schedules_nothing(self) -> None:
        manager = MagicMock()
        manager.db.commit = AsyncMock()
        manager.create_key = AsyncMock(side_effect=ValueError("duplicate"))
        tasks = BackgroundTasks()
        with pytest.raises(Exception):  # noqa: B017 — the HTTP 400
            await api_keys_routes.create_api_key(
                api_keys_routes.APIKeyCreate(name="deploy"),
                _request(),
                tasks,
                {"user_id": "owner-1"},
                manager=manager,
            )
        assert tasks.tasks == []

    @pytest.mark.asyncio
    async def test_regenerate_schedules(self) -> None:
        old = SimpleNamespace(
            id=7,
            name="ci",
            user_id="owner-1",
            workspace_id=WS,
            expires_at=None,
            revoked_at=None,
            agent_id=None,
            bound_context_id=None,
        )
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old))
        )
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key("ci")))
        tasks = BackgroundTasks()

        await api_keys_routes.regenerate_api_key(
            7, _request(), tasks, {"user_id": "owner-1"}, manager=manager, db=db
        )

        ((args, kwargs),) = _notices(tasks)
        assert args == ("owner-1", "api_key_regenerated")
        assert kwargs["key_name"] == "ci"


async def _run_and_capture(monkeypatch, tasks: BackgroundTasks) -> str:
    """Run the scheduled notice with Resend mocked; return the email text."""
    import services.email_providers.resend as resend_module
    from services.email_providers.resend import ResendEmailService

    captured: dict = {}

    def _send(params):
        captured.update(params)
        return {"id": "msg-1"}

    monkeypatch.setattr(resend_module.resend.Emails, "send", _send)
    monkeypatch.setattr(
        sns, "get_email_service", lambda: ResendEmailService(api_key="re_x", from_email="n@x.test")
    )
    monkeypatch.setattr(sns, "get_redis_client", lambda: fakeredis.aioredis.FakeRedis())
    monkeypatch.setattr(
        sns, "resolve_deliverable_address", AsyncMock(return_value="owner@example.test")
    )
    monkeypatch.setattr(sns, "_get_session_factory", lambda: _fake_factory)
    for task in tasks.tasks:
        await task()
    return captured["text"]


@asynccontextmanager
async def _fake_factory() -> AsyncIterator[MagicMock]:
    yield MagicMock()


# ---------------------------------------------------------------------------
# /workspaces/{ws}/members/{user}/credentials
# ---------------------------------------------------------------------------


class TestMemberCredentialRoutes:
    @pytest.mark.asyncio
    async def test_admin_regenerate_names_the_admin(self, monkeypatch) -> None:
        monkeypatch.setattr(mc, "check_permission", AsyncMock())
        monkeypatch.setattr(mc, "MemberCredentialsService", MagicMock())
        old = SimpleNamespace(id=7, name="ws-key", revoked_at=None)
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old))
        )
        db.commit = AsyncMock()
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key("ws-key")))
        monkeypatch.setattr(mc, "APIKeyManager", lambda db: manager)
        tasks = BackgroundTasks()

        await mc.regenerate_api_key(WS, "member-1", _request(), tasks, {"user_id": "admin-1"}, db)

        ((args, kwargs),) = _notices(tasks)
        assert args == ("member-1", "api_key_regenerated")  # the OWNER is told
        assert kwargs["actor_user_id"] == "admin-1"
        assert kwargs["key_name"] == "ws-key"

    @pytest.mark.asyncio
    async def test_self_regenerate_has_no_actor(self, monkeypatch) -> None:
        monkeypatch.setattr(mc, "check_permission", AsyncMock())
        monkeypatch.setattr(mc, "MemberCredentialsService", MagicMock())
        old = SimpleNamespace(id=7, name="mine", revoked_at=None)
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old))
        )
        db.commit = AsyncMock()
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key("mine")))
        monkeypatch.setattr(mc, "APIKeyManager", lambda db: manager)
        tasks = BackgroundTasks()

        await mc.regenerate_api_key(WS, "member-1", _request(), tasks, {"user_id": "member-1"}, db)

        ((_, kwargs),) = _notices(tasks)
        assert kwargs["actor_user_id"] is None

    @pytest.mark.asyncio
    async def test_session_self_mint_schedules(self, monkeypatch) -> None:
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key("laptop")))
        monkeypatch.setattr(mc, "APIKeyManager", lambda db: manager)
        db = MagicMock()
        db.commit = AsyncMock()
        tasks = BackgroundTasks()
        session_user = {"user_id": "member-1", "sub": "member-1", "email": "m@example.test"}

        result = await mc.create_api_key(
            WS,
            "member-1",
            mc.CreateAPIKeyRequest(name="laptop"),
            _request(),
            tasks,
            session_user,
            db,
        )

        assert result["plaintext_key"] == PLAINTEXT_KEY
        ((args, kwargs),) = _notices(tasks)
        assert args == ("member-1", "api_key_created")
        assert kwargs["key_name"] == "laptop"
        assert kwargs["actor_user_id"] is None
        assert PLAINTEXT_KEY not in repr(kwargs)

    @pytest.mark.asyncio
    async def test_owner_provisioned_mint_names_the_owner(self, monkeypatch) -> None:
        monkeypatch.setattr(mc, "authorize_workspace_management", AsyncMock())
        monkeypatch.setattr(mc, "_require_downgrade_target", AsyncMock())
        monkeypatch.setattr(mc, "audit_programmatic_workspace_action", AsyncMock())
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key("svc")))
        monkeypatch.setattr(mc, "APIKeyManager", lambda db: manager)
        db = MagicMock()
        db.commit = AsyncMock()
        tasks = BackgroundTasks()

        await mc._owner_provisioned_mint(
            WS,
            "member-1",
            mc.CreateAPIKeyRequest(name="svc", expires_days=30),
            {"user_id": "owner-9"},
            db,
            _request(),
            tasks,
        )

        ((args, kwargs),) = _notices(tasks)
        assert args == ("member-1", "api_key_created")
        assert kwargs["actor_user_id"] == "owner-9"

    @pytest.mark.asyncio
    async def test_admin_oauth_secret_regenerate(self, monkeypatch, encryptor) -> None:
        service = MagicMock()
        service.check_can_manage = AsyncMock(return_value=True)
        monkeypatch.setattr(mc, "MemberCredentialsService", lambda db: service)
        app = SimpleNamespace(client_id="cid-1", client_name="Team Bot")
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=app))
        )
        db.commit = AsyncMock()
        tasks = BackgroundTasks()

        result = await mc.regenerate_oauth_secret(
            WS, "member-1", _request(), tasks, {"user_id": "admin-1"}, db
        )

        ((args, kwargs),) = _notices(tasks)
        assert args == ("member-1", "oauth_secret_regenerated")
        assert kwargs["client_name"] == "Team Bot"
        assert kwargs["actor_user_id"] == "admin-1"
        assert result.client_secret not in repr(kwargs)


# ---------------------------------------------------------------------------
# /oauth
# ---------------------------------------------------------------------------


class TestOAuthRoutes:
    @pytest.mark.asyncio
    async def test_owner_secret_regenerate(self, monkeypatch, encryptor) -> None:
        client = SimpleNamespace(
            id=1,
            client_id="cid-1",
            client_name="My App",
            redirect_uris=["https://cb.example/cb"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope="memory:read",
            token_endpoint_auth_method="client_secret_post",
            owner_id="owner-1",
            provider="custom",
            created_at=utcnow(),
            client_secret_hash="",
            plaintext_secret_encrypted=None,
            hidden_at=None,
            visibility_expires_at=None,
        )
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = client
        monkeypatch.setattr(oauth_routes, "get_sync_session", lambda: session)
        monkeypatch.setattr(oauth_routes, "get_current_user_id", lambda request: "owner-1")
        tasks = BackgroundTasks()

        response = await oauth_routes.regenerate_oauth2_client_secret(
            _request(), tasks, "cid-1", {"user_id": "owner-1"}
        )

        ((args, kwargs),) = _notices(tasks)
        assert args == ("owner-1", "oauth_secret_regenerated")
        assert kwargs["client_name"] == "My App"
        assert response.client_secret not in repr(kwargs)

    def _device(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=5,
            client_id="cli-client",
            user_id=None,
            authorized_at=None,
            denied_at=None,
            is_expired=lambda: False,
        )

    def _device_session(self, monkeypatch, device) -> MagicMock:
        session = MagicMock()
        query = session.query.return_value.filter_by.return_value
        query.with_for_update.return_value.first.return_value = device
        monkeypatch.setattr(oauth_routes, "get_sync_session", lambda: session)
        monkeypatch.setattr(
            oauth_routes, "_get_user_from_session", lambda request: {"user_id": "u-1"}
        )
        # The approving browser session is still there (#1770 re-check).
        monkeypatch.setattr(oauth_routes, "browser_session_is_live", lambda sid, uid: True)
        return session

    @pytest.mark.asyncio
    async def test_every_device_approval_notifies(self, monkeypatch) -> None:
        # A device code can be phished, so approvals are not limited to the
        # first grant (unlike consent); no first-time lookup runs at all.
        check = MagicMock()
        monkeypatch.setattr(oauth_routes, "is_new_client_authorization", check)
        for _ in range(2):
            self._device_session(monkeypatch, self._device())
            tasks = BackgroundTasks()

            result = await oauth_routes.device_confirm(
                _request(), oauth_routes.DeviceConfirmRequest(user_code="ABCD1234"), tasks
            )

            assert result.status == "approved"
            ((args, kwargs),) = _notices(tasks)
            assert args == ("u-1", "oauth_client_authorized")
            assert kwargs["client_id"] == "cli-client"
        check.assert_not_called()

    @pytest.mark.asyncio
    async def test_device_denial_notifies_nothing(self, monkeypatch) -> None:
        self._device_session(monkeypatch, self._device())
        tasks = BackgroundTasks()
        result = await oauth_routes.device_confirm(
            _request(),
            oauth_routes.DeviceConfirmRequest(user_code="ABCD1234", approve=False),
            tasks,
        )
        assert result.status == "denied"
        assert tasks.tasks == []

    @pytest.mark.asyncio
    async def test_create_client_notifies_the_owner(self, monkeypatch, encryptor) -> None:
        session = MagicMock()

        def _refresh(client):
            client.id = 3
            client.created_at = utcnow()

        session.refresh.side_effect = _refresh
        monkeypatch.setattr(oauth_routes, "get_sync_session", lambda: session)
        monkeypatch.setattr(oauth_routes, "get_current_user_id", lambda request: "owner-1")
        tasks = BackgroundTasks()
        data = oauth_routes.OAuth2ClientCreateRequest(
            client_name="My Connector", redirect_uris=["https://cb.example/cb"]
        )

        response = await oauth_routes.create_oauth2_client(
            _request(), tasks, data, {"user_id": "owner-1"}
        )

        session.commit.assert_called_once()
        ((args, kwargs),) = _notices(tasks)
        assert args == ("owner-1", "oauth_client_created")
        assert kwargs["client_name"] == "My Connector"
        assert response.client_secret not in repr(kwargs)

    def _authorize(self, monkeypatch, *, first: bool, location: str) -> SimpleNamespace:
        request = _request()
        request.query_params = {
            "client_id": "cid-7",
            "redirect_uri": "https://cb.example/cb",
            "state": "s",
            "scope": "memory:read",
        }
        request.state.form_data = {"confirm": "yes"}
        monkeypatch.setattr(oauth_routes, "preload_form", AsyncMock())
        monkeypatch.setattr(
            oauth_routes,
            "get_current_user_from_session",
            lambda request: oauth_routes._OAuthUser(user_id="u-7", email="u7@example.test"),
        )
        monkeypatch.setattr(oauth_routes, "get_sync_session", MagicMock())
        monkeypatch.setattr(
            oauth_routes, "_validate_authorize_redirect_uri", lambda *args, **kwargs: True
        )
        self.checked: list[tuple] = []

        def _consent_is_new(client_id, user_id, scope):
            self.checked.append((client_id, user_id, scope))
            return first

        monkeypatch.setattr(oauth_routes, "_consent_is_new", _consent_is_new)
        monkeypatch.setattr(
            oauth_routes,
            "_run_oauth_sync",
            lambda *args, **kwargs: SimpleNamespace(location=location),
        )
        return request

    @pytest.mark.parametrize(
        ("first", "location", "expected"),
        [
            (True, "https://cb.example/cb?code=abc&state=s", 1),
            (False, "https://cb.example/cb?code=abc&state=s", 0),
            (True, "https://cb.example/cb?error=access_denied&state=s", 0),
        ],
    )
    @pytest.mark.asyncio
    async def test_consent_notifies_only_a_new_successful_grant(
        self, monkeypatch, first, location, expected
    ) -> None:
        request = self._authorize(monkeypatch, first=first, location=location)
        tasks = BackgroundTasks()

        response = await oauth_routes.oauth_authorize_post(request, tasks)

        assert response.status_code == 303
        # The check gets the requested scope (a broader scope is a new grant).
        assert self.checked == [("cid-7", "u-7", "memory:read")]
        notices = _notices(tasks)
        assert len(notices) == expected
        if notices:
            ((args, kwargs),) = notices
            assert args == ("u-7", "oauth_client_authorized")
            assert kwargs["client_id"] == "cid-7"


# ---------------------------------------------------------------------------
# Real Postgres: first-time client check and actor label
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def oauth_rows(db_session: AsyncSession) -> AsyncIterator[dict]:
    suffix = uuid4().hex[:10]
    rows = {"client_id": f"sn-client-{suffix}", "user_id": f"sn-user-{suffix}"}
    db_session.add(
        OAuth2Client(
            client_id=rows["client_id"],
            client_secret_hash="0" * 64,
            client_name="Notice Test",
            redirect_uris=["https://cb.example/cb"],
            grant_types=["authorization_code"],
            scope="memory:read memory:write",
        )
    )
    await db_session.commit()
    yield rows
    await db_session.rollback()
    for model in (OAuth2Token, OAuth2AuthorizationCode, OAuth2DeviceCode):
        await db_session.execute(delete(model).where(model.client_id == rows["client_id"]))
    await db_session.execute(
        delete(OAuth2Client).where(OAuth2Client.client_id == rows["client_id"])
    )
    await db_session.commit()


async def _is_first(db: AsyncSession, rows: dict, scope: str | None = "memory:read") -> bool:
    return await db.run_sync(
        lambda session: sns.is_new_client_authorization(
            session, client_id=rows["client_id"], user_id=rows["user_id"], scope=scope
        )
    )


def _token(rows: dict, *, scope: str | None = "memory:read", **kwargs) -> OAuth2Token:
    return OAuth2Token(
        client_id=rows["client_id"],
        user_id=kwargs.pop("user_id", rows["user_id"]),
        access_token=f"at-{uuid4().hex}",
        scope=scope,
        **kwargs,
    )


class TestFirstClientAuthorization:
    pytestmark = pytest.mark.asyncio(loop_scope="session")

    async def test_first_time(self, db_session: AsyncSession, oauth_rows) -> None:
        assert await _is_first(db_session, oauth_rows) is True

    async def test_revoked_token_still_counts(self, db_session: AsyncSession, oauth_rows) -> None:
        db_session.add(_token(oauth_rows, revoked=True))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is False

    async def test_pending_authorization_code_counts(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(
            OAuth2AuthorizationCode(
                code=f"code-{uuid4().hex}",
                client_id=oauth_rows["client_id"],
                user_id=oauth_rows["user_id"],
                redirect_uri="https://cb.example/cb",
                scope="memory:read",
                expires_at=utcnow(),
            )
        )
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is False

    async def test_approved_device_code_counts(self, db_session: AsyncSession, oauth_rows) -> None:
        device = OAuth2DeviceCode(
            device_code=f"dc-{uuid4().hex}",
            user_code=uuid4().hex[:8].upper(),
            client_id=oauth_rows["client_id"],
            user_id=oauth_rows["user_id"],
            scope="memory:read",
            expires_at=utcnow(),
            authorized_at=utcnow(),
        )
        db_session.add(device)
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is False

    async def test_another_users_grant_does_not_count(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, user_id="someone-else"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is True

    async def test_a_broader_scope_is_a_new_grant(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows, "memory:read") is False
        assert await _is_first(db_session, oauth_rows, "memory:read memory:write") is True

    async def test_scopes_granted_across_earlier_grants_add_up(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, scope="memory:read"))
        db_session.add(_token(oauth_rows, scope="memory:write"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows, "memory:write memory:read") is False

    async def test_no_requested_scope_means_the_registered_scope(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        # The client's rule grants its registered scope (read + write) then.
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows, None) is True

    async def test_a_scope_the_client_may_not_have_is_not_granted(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows, "memory:read memory:admin") is False

    async def test_a_client_changed_since_the_last_grant_is_a_new_grant(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is False

        client = (
            await db_session.execute(
                select(OAuth2Client).where(OAuth2Client.client_id == oauth_rows["client_id"])
            )
        ).scalar_one()
        client.redirect_uris = ["https://elsewhere.example/cb"]
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is True

        # A grant after the change settles it again.
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        assert await _is_first(db_session, oauth_rows) is False

    async def test_unknown_client_fails_toward_notifying(
        self, db_session: AsyncSession, oauth_rows
    ) -> None:
        db_session.add(_token(oauth_rows, scope="memory:read"))
        await db_session.commit()
        unknown = {"client_id": "no-such-client", "user_id": oauth_rows["user_id"]}
        assert await _is_first(db_session, unknown) is True


class TestAdminIsNamed:
    pytestmark = pytest.mark.asyncio(loop_scope="session")

    async def test_owner_email_names_the_admin(
        self, async_engine, db_session: AsyncSession, monkeypatch
    ) -> None:
        suffix = uuid4().hex[:8]
        owner = User(
            user_id=f"sn-owner-{suffix}",
            email=f"owner-{suffix}@notice.example",
            name="Owner",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            email_verified_at=utcnow(),  # verified on an OAuth sign-in
        )
        admin = User(
            user_id=f"sn-admin-{suffix}",
            email=f"admin-{suffix}@notice.example",
            name="Ada Admin",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
        )
        db_session.add_all([owner, admin])
        db_session.add(
            UserOAuthProvider(user_id=owner.user_id, provider="google", oauth_sub=f"s-{suffix}")
        )
        await db_session.commit()
        email = AsyncMock()
        email.send_security_notification = AsyncMock(return_value=True)
        fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(sns, "get_redis_client", lambda: fake)
        try:
            await sns.notify_security_event(
                owner.user_id,
                sns.SecurityEvent.API_KEY_REGENERATED,
                ip="192.0.2.1",
                user_agent="UA",
                key_name="ws-key",
                actor_user_id=admin.user_id,
                session_factory=async_sessionmaker(
                    async_engine, class_=AsyncSession, expire_on_commit=False
                ),
                email_service=email,
            )

            kwargs = email.send_security_notification.await_args.kwargs
            assert kwargs["to_email"] == owner.email
            (occurrence,) = kwargs["occurrences"]
            assert occurrence.actor is not None
            assert occurrence.actor.startswith("Ada Admin (admin-")
            assert "notice[.]example" in occurrence.actor
        finally:
            await db_session.execute(
                delete(UserOAuthProvider).where(UserOAuthProvider.user_id == owner.user_id)
            )
            await db_session.execute(
                delete(User).where(User.user_id.in_([owner.user_id, admin.user_id]))
            )
            await db_session.commit()


# ---------------------------------------------------------------------------
# Connector KMC keys, provider linking, MCP spawn
# ---------------------------------------------------------------------------


class TestConnectorKeys:
    def _create_result(self, key_name: str | None) -> MagicMock:
        result = MagicMock()
        result.connector.id = uuid4()
        result.connector.connector_type = "slack"
        result.connector.app_key = "default"
        result.resource_id = "slack_general"
        result.context_id = uuid4()
        result.plaintext_kmc_api_key = PLAINTEXT_KEY if key_name else None
        result.kmc_api_key_name = key_name
        result.token.id = 1
        result.plaintext_token = "kagura_resource_x"
        result.token.quota_events_per_hour = 1000
        return result

    @pytest.mark.parametrize("key_name", ["connector:abc", None])
    @pytest.mark.asyncio
    async def test_register_notifies_when_a_key_was_minted(self, monkeypatch, key_name) -> None:
        from api.routes import workspace_connectors as wc

        service = MagicMock()
        service.provision_connector = AsyncMock(return_value=self._create_result(key_name))
        monkeypatch.setattr(wc, "ConnectorProvisioningService", lambda db: service)
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        tasks = BackgroundTasks()

        await wc.create_workspace_connector(
            wc.WorkspaceConnectorCreateRequest(connector_type="slack", resource_id="slack_general"),
            _request(),
            tasks,
            {"user_id": "admin-1", "current_workspace_id": WS},
            db,
        )

        notices = _notices(tasks)
        if key_name is None:
            assert notices == []
            return
        ((args, kwargs),) = notices
        assert args == ("admin-1", "api_key_created")
        assert kwargs["key_name"] == "connector:abc"
        assert kwargs["actor_user_id"] is None
        assert PLAINTEXT_KEY not in repr(kwargs)

    @pytest.mark.parametrize(("owner", "actor"), [("owner-1", "admin-1"), ("admin-1", None)])
    @pytest.mark.asyncio
    async def test_rotate_notifies_the_key_owner(self, monkeypatch, owner, actor) -> None:
        from api.routes import workspace_connectors as wc
        from services.connector_provisioning import KmcKeyRotationResult

        service = MagicMock()
        service.rotate_kmc_key = AsyncMock(
            return_value=KmcKeyRotationResult(
                plaintext_kmc_api_key=PLAINTEXT_KEY,
                expires_at=utcnow(),
                config_version=2,
                key_owner_user_id=owner,
                key_name="connector:c1",
            )
        )
        monkeypatch.setattr(wc, "ConnectorProvisioningService", lambda db: service)
        db = MagicMock()
        db.commit = AsyncMock()
        tasks = BackgroundTasks()

        await wc.rotate_connector_kmc_key(
            uuid4(), _request(), tasks, {"user_id": "admin-1", "current_workspace_id": WS}, db
        )

        ((args, kwargs),) = _notices(tasks)
        assert args == (owner, "api_key_regenerated")
        assert kwargs["key_name"] == "connector:c1"
        assert kwargs["actor_user_id"] == actor
        assert PLAINTEXT_KEY not in repr(kwargs)

    @pytest.mark.asyncio
    async def test_failed_rotation_notifies_nothing(self, monkeypatch) -> None:
        from api.routes import workspace_connectors as wc
        from utils.exceptions import NotFoundException

        service = MagicMock()
        service.rotate_kmc_key = AsyncMock(side_effect=NotFoundException("Connector"))
        monkeypatch.setattr(wc, "ConnectorProvisioningService", lambda db: service)
        db = MagicMock()
        db.rollback = AsyncMock()
        tasks = BackgroundTasks()
        with pytest.raises(Exception):  # noqa: B017 — the HTTP 404
            await wc.rotate_connector_kmc_key(
                uuid4(), _request(), tasks, {"user_id": "admin-1", "current_workspace_id": WS}, db
            )
        assert tasks.tasks == []


class TestProviderLinking:
    def _session_manager(self, suffix: str) -> MagicMock:
        values = {
            f"oauth2_state_intent:{suffix}": "link",
            f"oauth2_state_user:{suffix}": "u-link",
        }
        redis = MagicMock()
        redis.get.side_effect = lambda key: values.get(key)
        manager = MagicMock()
        manager._redis = redis
        return manager

    @pytest.mark.parametrize(("newly_linked", "expected"), [(True, 1), (False, 0)])
    @pytest.mark.asyncio
    async def test_link_callback_notifies_a_new_link(
        self, monkeypatch, newly_linked, expected
    ) -> None:
        from api.routes import auth as auth_module

        suffix = uuid4().hex[:8]
        monkeypatch.setattr(auth_module, "_session_manager", self._session_manager(suffix))
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(email="u@example.test")
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        async def _get_db():
            yield db

        monkeypatch.setattr(auth_module, "get_db", _get_db)
        service = MagicMock()
        service.link = AsyncMock(return_value=newly_linked)
        monkeypatch.setattr(auth_module, "AccountLinkingService", lambda db: service)

        response = await auth_module._maybe_link_redirect(
            state=suffix,
            provider="github",
            idp_sub="gh-1",
            idp_email="u@example.test",
            ip_address="198.51.100.7",
            user_agent="pytest-link",
        )

        assert response is not None and response.status_code == 303
        notices = _notices(response.background)
        assert len(notices) == expected
        if notices:
            ((args, kwargs),) = notices
            assert args == ("u-link", "sign_in_method_added")
            assert kwargs["sign_in_method"] == "GitHub sign-in"
            assert kwargs["ip"] == "198.51.100.7"
            assert kwargs["user_agent"] == "pytest-link"


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_runs_the_notice_without_background_tasks(self, monkeypatch) -> None:
        import asyncio

        notify = AsyncMock()
        monkeypatch.setattr(sns, "notify_security_event", notify)
        sns.spawn_security_notification(
            user_id="u-mcp", event=sns.SecurityEvent.API_KEY_CREATED, key_name="connector:x"
        )
        await asyncio.gather(*list(sns._spawned))
        assert notify.await_args.args == ("u-mcp", sns.SecurityEvent.API_KEY_CREATED)
        assert notify.await_args.kwargs["key_name"] == "connector:x"
        assert notify.await_args.kwargs["ip"] is None

    def test_schedule_never_raises(self) -> None:
        tasks = MagicMock()
        tasks.add_task.side_effect = RuntimeError("boom")
        sns.schedule_security_notification(
            tasks, user_id="u", event=sns.SecurityEvent.PASSWORD_CHANGED, request=_request()
        )  # does not raise


# ---------------------------------------------------------------------------
# Real Postgres: device approval commits even when the notice cannot be queued
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from config.database import to_sync_database_url
    from tests.conftest import TEST_DATABASE_URL

    engine = create_engine(to_sync_database_url(TEST_DATABASE_URL))
    try:
        with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"Test database not available (sync): {exc}")
    yield sessionmaker(engine)
    engine.dispose()


class TestDeviceApprovalCommitsDespiteNoticeFailure:
    pytestmark = pytest.mark.asyncio(loop_scope="session")

    async def test_approval_commits_when_scheduling_raises(
        self, db_session: AsyncSession, oauth_rows, sync_session_factory, monkeypatch
    ) -> None:
        user_code = uuid4().hex[:8].upper()
        db_session.add(
            OAuth2DeviceCode(
                device_code=f"dc-{uuid4().hex}",
                user_code=user_code,
                client_id=oauth_rows["client_id"],
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        await db_session.commit()
        monkeypatch.setattr(oauth_routes, "get_sync_session", sync_session_factory)
        monkeypatch.setattr(
            oauth_routes,
            "_get_user_from_session",
            lambda request: {"user_id": oauth_rows["user_id"]},
        )
        monkeypatch.setattr(oauth_routes, "browser_session_is_live", lambda sid, uid: True)
        tasks = MagicMock()
        tasks.add_task.side_effect = RuntimeError("queue broken")

        result = await oauth_routes.device_confirm(
            _request(), oauth_routes.DeviceConfirmRequest(user_code=user_code), tasks
        )

        assert result.status == "approved"
        tasks.add_task.assert_called_once()  # the notice was attempted
        db_session.expire_all()
        row = (
            await db_session.execute(
                select(OAuth2DeviceCode).where(OAuth2DeviceCode.user_code == user_code)
            )
        ).scalar_one()
        assert row.authorized_at is not None
        assert row.user_id == oauth_rows["user_id"]


# ---------------------------------------------------------------------------
# gate2 follow-ups: first-grant check wrapper, commit-before-notice order
# ---------------------------------------------------------------------------


class TestConsentIsNew:
    def test_db_error_fails_open_to_notifying(self, monkeypatch) -> None:
        session = MagicMock()
        monkeypatch.setattr(oauth_routes, "get_sync_session", lambda: session)
        monkeypatch.setattr(
            oauth_routes, "is_new_client_authorization", MagicMock(side_effect=OSError("db"))
        )
        assert oauth_routes._consent_is_new("cid", "u-1", "memory:read") is True
        session.close.assert_called_once()

    @pytest.mark.parametrize(("client_id", "user_id"), [(None, "u-1"), ("cid", None), ("", "")])
    def test_missing_ids_never_notify(self, monkeypatch, client_id, user_id) -> None:
        opened = MagicMock()
        monkeypatch.setattr(oauth_routes, "get_sync_session", opened)
        assert oauth_routes._consent_is_new(client_id, user_id, None) is False
        opened.assert_not_called()

    def test_passes_the_answer_through(self, monkeypatch) -> None:
        monkeypatch.setattr(oauth_routes, "get_sync_session", MagicMock)
        check = MagicMock(return_value=False)
        monkeypatch.setattr(oauth_routes, "is_new_client_authorization", check)
        assert oauth_routes._consent_is_new("cid", "u-1", "memory:read") is False
        assert check.call_args.kwargs == {
            "client_id": "cid",
            "user_id": "u-1",
            "scope": "memory:read",
        }


class TestCommitBeforeNotice:
    """The notice is scheduled only after the change's commit."""

    @pytest.mark.asyncio
    async def test_api_key_create(self) -> None:
        order = MagicMock()
        manager = MagicMock()
        manager.create_key = AsyncMock(return_value=(PLAINTEXT_KEY, _new_key()))
        manager.db.commit = AsyncMock(side_effect=lambda: order.commit())
        tasks = BackgroundTasks()
        real_add = tasks.add_task
        tasks.add_task = lambda *a, **k: (order.schedule(), real_add(*a, **k))  # type: ignore[method-assign]

        await api_keys_routes.create_api_key(
            api_keys_routes.APIKeyCreate(name="deploy"),
            _request(),
            tasks,
            {"user_id": "owner-1"},
            manager=manager,
        )

        assert [c[0] for c in order.mock_calls] == ["commit", "schedule"]

    @pytest.mark.asyncio
    async def test_password_change(self, monkeypatch) -> None:
        from api.routes import password as password_routes

        order = MagicMock()

        async def _change(**kwargs):
            order.commit()  # the service commits inside change()

        service = MagicMock()
        service.change = AsyncMock(side_effect=_change)
        monkeypatch.setattr(password_routes, "PasswordAccountService", lambda db: service)
        monkeypatch.setattr(password_routes, "increment_counter", AsyncMock(return_value=1))
        tasks = BackgroundTasks()
        real_add = tasks.add_task
        tasks.add_task = lambda *a, **k: (order.schedule(), real_add(*a, **k))  # type: ignore[method-assign]

        await password_routes.change_password(
            password_routes.PasswordChangeBody(current_password="a", new_password="b"),
            _request(),
            {"user_id": "u-1"},
            db=None,
            background_tasks=tasks,
        )

        assert [c[0] for c in order.mock_calls] == ["commit", "schedule"]
        ((args, _),) = _notices(tasks)
        assert args == ("u-1", "password_changed")


class TestFirstAuthorizationSessionFailure:
    def test_session_acquisition_failure_fails_open(self, monkeypatch) -> None:
        monkeypatch.setattr(
            oauth_routes, "get_sync_session", MagicMock(side_effect=OSError("pool exhausted"))
        )
        assert oauth_routes._consent_is_new("cid", "u-1", None) is True
