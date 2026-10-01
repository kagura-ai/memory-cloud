"""New-device sign-in detection (#1769)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response

from config.settings import Settings
from services import known_device_service as svc
from services.known_device_service import (
    DEVICE_COOKIE_MAX_AGE,
    DEVICE_COOKIE_NAME,
    SignIn,
    device_hash,
    new_device_cookie_value,
    note_browser_sign_in,
    read_device_cookie,
    record_sign_in,
    set_device_cookie,
)
from services.security_notification_service import SecurityEvent

KEY = "k" * 32


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    cfg = Settings(_env_file=None, audit_hmac_key=KEY, known_device_max_per_user=3)
    monkeypatch.setattr(svc, "get_settings", lambda: cfg)
    return cfg


# ---------------------------------------------------------------------------
# Cookie value and hash
# ---------------------------------------------------------------------------


class TestCookie:
    def test_new_value_is_url_safe_and_long(self) -> None:
        value = new_device_cookie_value()
        assert read_device_cookie(SimpleNamespace(cookies={DEVICE_COOKIE_NAME: value})) == value
        assert len(value) >= 32

    def test_read_rejects_a_malformed_cookie(self) -> None:
        for bad in ("", "short", "has space" * 5, "x" * 300, "é" * 40):
            assert read_device_cookie(SimpleNamespace(cookies={DEVICE_COOKIE_NAME: bad})) is None

    def test_read_without_cookie(self) -> None:
        assert read_device_cookie(SimpleNamespace(cookies={})) is None
        assert read_device_cookie(SimpleNamespace()) is None

    def test_hash_is_keyed_and_domain_separated(self) -> None:
        value = new_device_cookie_value()
        digest = device_hash(value)
        assert len(digest) == 64
        assert digest == device_hash(value)
        assert digest != device_hash(value + "x")
        # Not the bare HMAC of the value: another use of the same key over the
        # same string does not collide with a device hash.
        from utils.hashing import hmac_sha256_hex

        assert digest != hmac_sha256_hex(value, KEY)

    def test_set_cookie_attributes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "production")
        response = Response()
        set_device_cookie(response, "v" * 43)
        header = response.headers["set-cookie"]
        assert header.startswith(f"{DEVICE_COOKIE_NAME}=" + "v" * 43)
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=lax" in header
        assert f"Max-Age={DEVICE_COOKIE_MAX_AGE}" in header
        assert "Path=/" in header


# ---------------------------------------------------------------------------
# record_sign_in — the DB rule
# ---------------------------------------------------------------------------


def _db(*, armed: bool, inserted: bool) -> tuple[AsyncMock, SimpleNamespace]:
    """A session whose statements answer in order: user lock, upsert, prune."""
    user = SimpleNamespace(known_devices_since=datetime(2026, 1, 1) if armed else None)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = user
    upsert_result = MagicMock()
    upsert_result.one.return_value = ("row-id", inserted)
    prune_result = MagicMock()
    prune_result.rowcount = 0
    db = AsyncMock()
    db.execute.side_effect = [lock_result, upsert_result, prune_result]
    return db, user


@pytest.mark.asyncio
async def test_known_device_is_one_upsert_that_updated() -> None:
    db, user = _db(armed=True, inserted=False)

    outcome = await record_sign_in(db, user_id="u1", digest="d" * 64, now=datetime(2026, 10, 1))

    assert outcome == SignIn.KNOWN
    assert db.execute.await_count == 2  # lock + upsert, no prune
    assert user.known_devices_since == datetime(2026, 1, 1)


@pytest.mark.asyncio
async def test_first_device_arms_the_account_without_alert() -> None:
    db, user = _db(armed=False, inserted=True)
    now = datetime(2026, 10, 1, 12)

    outcome = await record_sign_in(db, user_id="u1", digest="d" * 64, now=now)

    assert outcome == SignIn.FIRST_DEVICE
    assert user.known_devices_since == now
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_unknown_device_on_an_armed_account_is_new_and_prunes() -> None:
    db, _ = _db(armed=True, inserted=True)

    outcome = await record_sign_in(db, user_id="u1", digest="d" * 64, now=datetime(2026, 10, 1))

    assert outcome == SignIn.NEW_DEVICE
    assert db.execute.await_count == 3  # lock + upsert + prune to the cap


@pytest.mark.asyncio
async def test_after_a_reset_the_first_sign_in_back_is_reported() -> None:
    # The reset deleted the rows but left known_devices_since set: an
    # attacker who signs in first is not treated as the account's first device.
    db, _ = _db(armed=True, inserted=True)

    outcome = await record_sign_in(db, user_id="u1", digest="x" * 64, now=datetime(2026, 10, 1))

    assert outcome == SignIn.NEW_DEVICE


def test_upsert_locks_the_user_row_and_returns_whether_it_inserted() -> None:
    # Statement shapes, compiled for PostgreSQL.
    from sqlalchemy.dialects import postgresql

    db, _ = _db(armed=True, inserted=True)
    import asyncio

    asyncio.run(record_sign_in(db, user_id="u1", digest="d" * 64, now=datetime(2026, 10, 1)))
    lock_stmt, upsert_stmt, prune_stmt = (c.args[0] for c in db.execute.await_args_list)
    lock_sql = str(lock_stmt.compile(dialect=postgresql.dialect()))
    upsert_sql = str(upsert_stmt.compile(dialect=postgresql.dialect()))
    prune_sql = str(prune_stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in lock_sql
    assert "ON CONFLICT ON CONSTRAINT user_known_devices_user_device_key DO UPDATE" in upsert_sql
    assert "RETURNING user_known_devices.id, (xmax = 0)" in upsert_sql
    assert "DELETE FROM user_known_devices" in prune_sql and "LIMIT" in prune_sql


# ---------------------------------------------------------------------------
# note_browser_sign_in — the orchestration at a sign-in route
# ---------------------------------------------------------------------------


def _request(cookie: str | None, ip: str = "203.0.113.5") -> SimpleNamespace:
    cookies = {DEVICE_COOKIE_NAME: cookie} if cookie else {}
    return SimpleNamespace(
        cookies=cookies, headers={"user-agent": "pytest UA"}, client=SimpleNamespace(host=ip)
    )


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    db = AsyncMock()

    async def _get_db():
        yield db

    record = AsyncMock(return_value=SignIn.KNOWN)
    spawn = MagicMock()
    monkeypatch.setattr(svc, "get_db", _get_db)
    monkeypatch.setattr(svc, "record_sign_in", record)
    # The notice module is imported lazily inside note_browser_sign_in.
    import services.security_notification_service as notices

    monkeypatch.setattr(notices, "spawn_security_notification", spawn)
    return {"db": db, "record": record, "spawn": spawn}


@pytest.mark.asyncio
async def test_known_device_sets_no_alert_and_keeps_the_cookie(pipeline) -> None:
    cookie = new_device_cookie_value()
    response = Response()

    await note_browser_sign_in(_request(cookie), response, user_id="u1", sign_in_method="Password")

    pipeline["record"].assert_awaited_once()
    assert pipeline["record"].await_args.kwargs["digest"] == device_hash(cookie)
    pipeline["spawn"].assert_not_called()
    # The same cookie is re-issued so its lifetime slides.
    assert response.headers["set-cookie"].startswith(f"{DEVICE_COOKIE_NAME}={cookie}")
    pipeline["db"].commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_device_alerts_with_ip_ua_and_method(pipeline) -> None:
    pipeline["record"].return_value = SignIn.NEW_DEVICE
    response = Response()

    await note_browser_sign_in(_request(None), response, user_id="u1", sign_in_method="Google")

    pipeline["spawn"].assert_called_once()
    kwargs = pipeline["spawn"].call_args.kwargs
    assert kwargs["user_id"] == "u1"
    assert kwargs["event"] == SecurityEvent.NEW_DEVICE_SIGN_IN
    # IP and user agent are read from the request by the notice pipeline.
    assert kwargs["request"].client.host == "203.0.113.5"
    assert kwargs["request"].headers["user-agent"] == "pytest UA"
    assert kwargs["sign_in_method"] == "Google"
    # No cookie came in: a fresh one is minted and its hash is what was stored.
    issued = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert pipeline["record"].await_args.kwargs["digest"] == device_hash(issued)


@pytest.mark.asyncio
async def test_first_device_is_silent(pipeline) -> None:
    pipeline["record"].return_value = SignIn.FIRST_DEVICE

    await note_browser_sign_in(_request(None), Response(), user_id="u1", sign_in_method="GitHub")

    pipeline["spawn"].assert_not_called()


@pytest.mark.asyncio
async def test_malformed_cookie_is_replaced(pipeline) -> None:
    response = Response()

    await note_browser_sign_in(
        _request("not a valid cookie!"), response, user_id="u1", sign_in_method="Password"
    )

    issued = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert issued != "not a valid cookie!"
    assert pipeline["record"].await_args.kwargs["digest"] == device_hash(issued)


@pytest.mark.asyncio
async def test_a_db_failure_never_breaks_the_sign_in(pipeline) -> None:
    pipeline["record"].side_effect = RuntimeError("db down")
    response = Response()

    await note_browser_sign_in(_request(None), response, user_id="u1", sign_in_method="Password")

    pipeline["spawn"].assert_not_called()
    pipeline["db"].rollback.assert_awaited_once()
    # The cookie is still issued: the next sign-in can recognize the browser.
    assert DEVICE_COOKIE_NAME in response.headers["set-cookie"]


def test_retention_cutoff_uses_settings(settings: Settings) -> None:
    now = datetime(2026, 10, 1)
    stmt = svc.stale_devices_delete(now - timedelta(days=settings.known_device_retention_days))
    assert "user_known_devices" in str(stmt)
    assert "last_seen" in str(stmt)
