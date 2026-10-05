"""The operator CLI password reset against real Postgres (Issue #1866).

``python -m src.cli.reset_password`` (choices 1 and 3) is run end to end on
its own synchronous engine, with only the prompts and the browser-session
store (Redis) replaced:

- the new password is stored;
- every OAuth2 / MCP token of the account is revoked, and the timestamps of a
  pair that was already rotated or revoked are preserved;
- pending authorization codes and device codes are deleted;
- known devices are forgotten and emailed password links invalidated;
- the sessions are deleted with ``strict=True`` while the transaction is
  still open — another connection sees none of the writes at that point;
- when the session delete fails, nothing is committed.

Another account's grants are never touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.oauth2_bearer import find_active_oauth_token
from auth.password import hash_password, verify_password
from cli import reset_password
from config.database import to_sync_database_url
from models.auth import (
    EmailActionToken,
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
    User,
    UserKnownDevice,
)
from services.email_action_token_service import EmailActionTokenService
from tests.conftest import TEST_DATABASE_URL
from utils.datetime import utcnow

OLD = "Old-Password-123!"
NEW = "New-Password-456!"

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _StoreDown(Exception):
    """The session store failed while deleting the sessions."""


@dataclass
class _Account:
    user_id: str
    login_id: str
    live_token: str
    rotated_token: str
    rotated_at: datetime


async def _add_grants(db: AsyncSession, client_id: str, user_id: str) -> str:
    """A live token, a pending code and an approved device code; returns the token."""
    suffix = uuid4().hex
    db.add(
        OAuth2Token(
            client_id=client_id,
            user_id=user_id,
            access_token=f"at-{suffix}",
            refresh_token=f"rt-{suffix}",
            scope="memory:read",
            expires_in=3600,
        )
    )
    db.add(
        OAuth2AuthorizationCode(
            code=f"code-{suffix}",
            client_id=client_id,
            user_id=user_id,
            redirect_uri="https://client.example/cb",
            scope="memory:read",
            expires_at=utcnow() + timedelta(minutes=5),
        )
    )
    db.add(
        OAuth2DeviceCode(
            device_code=f"dc-{suffix}",
            user_code=suffix[:8].upper(),
            client_id=client_id,
            user_id=user_id,
            scope="memory:read",
            expires_at=utcnow() + timedelta(minutes=10),
            authorized_at=utcnow(),
        )
    )
    return f"at-{suffix}"


async def _add_user(db: AsyncSession, *, totp: bool = False) -> tuple[str, str]:
    suffix = uuid4().hex[:10]
    user_id, login_id = f"local:cli-{suffix}", f"cli-{suffix}"
    db.add(
        User(
            user_id=user_id,
            email=f"{login_id}@local",
            name="CLI reset",
            role="admin",
            is_initial_admin=False,
            auth_method="password",
            login_id=login_id,
            password_hash=hash_password(OLD),
            totp_enabled=totp,
            totp_secret="encrypted-secret" if totp else None,
        )
    )
    return user_id, login_id


@pytest_asyncio.fixture(loop_scope="session")
async def accounts(db_session: AsyncSession) -> AsyncIterator[tuple[_Account, _Account]]:
    """The account being reset and a bystander, each with a full set of grants."""
    client_id = f"cli-reset-{uuid4().hex[:10]}"
    db_session.add(
        OAuth2Client(
            client_id=client_id,
            client_secret_hash="",
            client_name="CLI reset test client",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="memory:read",
            redirect_uris=["https://client.example/cb"],
            token_endpoint_auth_method="none",
            provider="claude",
        )
    )
    made: list[_Account] = []
    for totp in (True, False):
        user_id, login_id = await _add_user(db_session, totp=totp)
        await db_session.flush()
        live = await _add_grants(db_session, client_id, user_id)
        # A pair rotated three days ago: its timestamps are history to keep.
        rotated_at = utcnow() - timedelta(days=3)
        rotated = f"at-old-{uuid4().hex}"
        db_session.add(
            OAuth2Token(
                client_id=client_id,
                user_id=user_id,
                access_token=rotated,
                refresh_token=f"rt-old-{uuid4().hex}",
                scope="memory:read",
                expires_in=3600,
                access_token_revoked_at=rotated_at,
                refresh_token_revoked_at=rotated_at,
            )
        )
        db_session.add(UserKnownDevice(user_id=user_id, device_hash=uuid4().hex * 2))
        await EmailActionTokenService(db_session).issue(
            user_id=user_id, email=f"{login_id}@local", purpose="reset_password"
        )
        made.append(_Account(user_id, login_id, live, rotated, rotated_at))
    await db_session.commit()

    yield made[0], made[1]

    await db_session.rollback()
    user_ids = [account.user_id for account in made]
    # Tokens and device codes cascade with the client, known devices and
    # email tokens with the user; authorization codes have no foreign key.
    await db_session.execute(
        delete(OAuth2AuthorizationCode).where(OAuth2AuthorizationCode.client_id == client_id)
    )
    await db_session.execute(delete(OAuth2Client).where(OAuth2Client.client_id == client_id))
    await db_session.execute(delete(User).where(User.user_id.in_(user_ids)))
    await db_session.commit()


@dataclass
class _State:
    password_is_new: bool
    totp_enabled: bool
    live_token_active: bool
    tokens_unrevoked: int
    authorization_codes: int
    device_codes: int
    known_devices: int
    password_links: int


async def _count(db: AsyncSession, model: type, *where: object) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _state(db: AsyncSession, account: _Account) -> _State:
    db.expire_all()
    uid = account.user_id
    user = (await db.execute(select(User).where(User.user_id == uid))).scalar_one()
    assert user.password_hash is not None
    state = _State(
        password_is_new=verify_password(NEW, user.password_hash),
        totp_enabled=bool(user.totp_enabled),
        live_token_active=await find_active_oauth_token(account.live_token, db) is not None,
        tokens_unrevoked=await _count(
            db,
            OAuth2Token,
            OAuth2Token.user_id == uid,
            OAuth2Token.refresh_token_revoked_at.is_(None),
        ),
        authorization_codes=await _count(
            db, OAuth2AuthorizationCode, OAuth2AuthorizationCode.user_id == uid
        ),
        device_codes=await _count(db, OAuth2DeviceCode, OAuth2DeviceCode.user_id == uid),
        known_devices=await _count(db, UserKnownDevice, UserKnownDevice.user_id == uid),
        password_links=await _count(
            db,
            EmailActionToken,
            EmailActionToken.user_id == uid,
            EmailActionToken.used_at.is_(None),
        ),
    )
    # ``find_active_oauth_token`` and the counts opened a transaction; end it
    # so this session holds no snapshot or lock while the CLI runs.
    await db.rollback()
    return state


_UNTOUCHED = _State(
    password_is_new=False,
    totp_enabled=True,
    live_token_active=True,
    tokens_unrevoked=1,
    authorization_codes=1,
    device_codes=1,
    known_devices=1,
    password_links=1,
)


def _run_cli(login_id: str, choice: str, delete_sessions: Callable[..., int]) -> MagicMock:
    """Run the CLI on its own sync engine against the test database."""
    manager = MagicMock()
    manager.delete_user_sessions.side_effect = delete_sessions
    answers = [login_id, choice] + (["n"] if choice == "3" else [])
    with (
        patch.object(
            reset_password,
            "get_sync_database_url",
            return_value=to_sync_database_url(TEST_DATABASE_URL),
        ),
        patch.object(reset_password, "SessionManager", MagicMock(return_value=manager)),
        patch("builtins.input", side_effect=answers),
        patch.object(reset_password.getpass, "getpass", side_effect=[NEW, NEW]),
    ):
        reset_password.reset_password()
    return manager


@pytest.mark.parametrize("choice", ["1", "3"])
async def test_cli_reset_revokes_grants_and_keeps_earlier_revocation_times(
    db_session: AsyncSession,
    accounts: tuple[_Account, _Account],
    async_engine,
    choice: str,
    capsys,
) -> None:
    target, bystander = accounts
    assert await _state(db_session, target) == _UNTOUCHED
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    seen_at_session_delete: list[_State] = []

    async def _observe() -> _State:
        async with factory() as other:
            return await _state(other, target)

    def _delete_sessions(user_id: str, *, strict: bool) -> int:
        # Runs in the CLI's thread while its transaction is open: another
        # connection must still see the account as it was (nothing committed
        # before the sessions are gone).
        assert user_id == target.user_id and strict is True
        future = asyncio.run_coroutine_threadsafe(_observe(), loop)
        seen_at_session_delete.append(future.result(timeout=30))
        return 2

    manager = await asyncio.to_thread(_run_cli, target.login_id, choice, _delete_sessions)

    manager.delete_user_sessions.assert_called_once_with(target.user_id, strict=True)
    assert seen_at_session_delete == [_UNTOUCHED]
    assert await _state(db_session, target) == _State(
        password_is_new=True,
        totp_enabled=choice == "1",
        live_token_active=False,
        tokens_unrevoked=0,
        authorization_codes=0,
        device_codes=0,
        known_devices=0,
        password_links=0,
    )

    rows = (
        (await db_session.execute(select(OAuth2Token).where(OAuth2Token.user_id == target.user_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # revoked, not deleted: the history survives
    by_token = {row.access_token: row for row in rows}
    rotated = by_token[target.rotated_token]
    assert rotated.access_token_revoked_at == target.rotated_at
    assert rotated.refresh_token_revoked_at == target.rotated_at
    live = by_token[target.live_token]
    assert live.revoked is True
    assert live.access_token_revoked_at is not None
    assert live.access_token_revoked_at > target.rotated_at
    assert live.refresh_token_revoked_at == live.access_token_revoked_at
    await db_session.rollback()

    # The other account keeps everything (its MFA was off from the start).
    untouched_bystander = _State(**{**_UNTOUCHED.__dict__, "totp_enabled": False})
    assert await _state(db_session, bystander) == untouched_bystander

    out = capsys.readouterr().out
    assert "2 browser session(s), 1 OAuth / MCP token(s)" in out
    assert "2 pending authorization / device code(s)" in out


async def test_cli_reset_commits_nothing_when_the_session_delete_fails(
    db_session: AsyncSession, accounts: tuple[_Account, _Account], capsys
) -> None:
    target, _bystander = accounts

    def _delete_sessions(user_id: str, *, strict: bool) -> int:
        raise _StoreDown("redis timeout")

    with pytest.raises(SystemExit) as exit_info:
        await asyncio.to_thread(_run_cli, target.login_id, "3", _delete_sessions)

    assert exit_info.value.code == 1
    assert await _state(db_session, target) == _UNTOUCHED
    out = capsys.readouterr().out
    assert "Password updated" not in out
    assert "rolled back" in out
