"""When each account of a session container signed in (#1803).

An identity link is proved by the browser session holding both accounts, and
an account stays in a session for as long as the session lives. The container
therefore records when each account last went through a sign-in, so a link can
ask for a recent one. Only a sign-in sets the time: switching the active
account, refreshing the TTL or updating the identity never does.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from auth.session import SessionManager
from tests.auth.test_session_container import USER, FakeRedis, raw
from utils.datetime import utcnow

OTHER = {"sub": "local:admin", "user_id": "local:admin", "email": "admin@local"}


@pytest.fixture
def manager(monkeypatch) -> SessionManager:
    fake = FakeRedis()
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    return SessionManager(redis_url="redis://fake:6379")


def _set_signed_in_at(
    manager: SessionManager, sid: str, account: str, when: str | None, key: str = "signed_in_at"
) -> None:
    stored = raw(manager, sid)
    if when is None:
        stored.get(key, {}).pop(account, None)
    else:
        stored.setdefault(key, {})[account] = when
    manager._redis.store[f"session:{sid}"] = json.dumps(stored)


class TestSignedInAt:
    def test_create_session_records_the_sign_in(self, manager):
        before = utcnow()
        sid = manager.create_session(USER)

        when = manager.signed_in_at(sid, "google_1")

        assert when is not None
        assert before - timedelta(seconds=1) <= when <= utcnow()

    def test_add_account_records_the_added_account_only(self, manager):
        sid = manager.create_session(USER)
        _set_signed_in_at(manager, sid, "google_1", "2026-01-01T00:00:00")

        assert manager.add_account(sid, OTHER)

        assert manager.signed_in_at(sid, "local:admin") is not None
        assert manager.signed_in_at(sid, "google_1").isoformat() == "2026-01-01T00:00:00"

    def test_signing_in_again_refreshes_the_time(self, manager):
        sid = manager.create_session(USER)
        manager.add_account(sid, OTHER)
        _set_signed_in_at(manager, sid, "local:admin", "2026-01-01T00:00:00")

        assert manager.add_account(sid, OTHER)

        assert manager.signed_in_at(sid, "local:admin") > utcnow() - timedelta(minutes=1)

    def test_switching_accounts_does_not_count_as_a_sign_in(self, manager):
        sid = manager.create_session(USER)
        manager.add_account(sid, OTHER)
        _set_signed_in_at(manager, sid, "google_1", "2026-01-01T00:00:00")

        assert manager.switch_account(sid, "google_1")

        assert manager.signed_in_at(sid, "google_1").isoformat() == "2026-01-01T00:00:00"

    def test_update_session_and_reads_do_not_touch_it(self, manager):
        sid = manager.create_session(USER)
        _set_signed_in_at(manager, sid, "google_1", "2026-01-01T00:00:00")

        manager.update_session(sid, {"role": "admin", "signed_in_at": {"google_1": "x"}})
        manager.get_session(sid)

        assert manager.signed_in_at(sid, "google_1").isoformat() == "2026-01-01T00:00:00"

    def test_it_is_not_part_of_the_identity_callers_see(self, manager):
        sid = manager.create_session(USER)

        session = manager.get_session(sid)

        assert "signed_in_at" not in session
        assert all("signed_in_at" not in a for a in manager.list_accounts(sid))

    def test_remove_account_drops_its_time(self, manager):
        sid = manager.create_session(USER)
        manager.add_account(sid, OTHER)

        assert manager.remove_account(sid, "local:admin")

        assert "local:admin" not in raw(manager, sid)["signed_in_at"]

    def test_a_session_from_before_the_record_has_no_time(self, manager):
        """Fail closed: a container written before this change, or a flat
        legacy record, has no sign-in time to offer."""
        sid = manager.create_session(USER)
        stored = raw(manager, sid)
        del stored["signed_in_at"]
        manager._redis.store[f"session:{sid}"] = json.dumps(stored)

        assert manager.signed_in_at(sid, "google_1") is None

        manager._redis.store["session:flat"] = json.dumps(
            {**USER, "created_at": "2026-01-01T00:00:00"}
        )
        assert manager.signed_in_at("flat", "google_1") is None

    def test_an_unreadable_time_is_no_time(self, manager):
        sid = manager.create_session(USER)
        _set_signed_in_at(manager, sid, "google_1", "not-a-date")

        assert manager.signed_in_at(sid, "google_1") is None

    def test_a_time_with_a_zone_is_no_time(self, manager):
        """Only naive UTC is ever written; anything else was not."""
        sid = manager.create_session(USER)
        _set_signed_in_at(manager, sid, "google_1", utcnow().isoformat() + "+00:00")

        assert manager.signed_in_at(sid, "google_1") is None

    def test_an_account_not_in_the_session_or_a_missing_session_has_none(self, manager):
        sid = manager.create_session(USER)

        assert manager.signed_in_at(sid, "local:admin") is None
        assert manager.signed_in_at("missing", "google_1") is None


class TestSignedInWithin:
    def test_a_recent_sign_in_counts(self, manager):
        sid = manager.create_session(USER)

        assert manager.signed_in_within(sid, "google_1", timedelta(minutes=10))

    def test_an_old_sign_in_does_not(self, manager):
        sid = manager.create_session(USER)
        old = (utcnow() - timedelta(minutes=11)).isoformat()
        _set_signed_in_at(manager, sid, "google_1", old)

        assert not manager.signed_in_within(sid, "google_1", timedelta(minutes=10))

    def test_a_time_in_the_future_does_not(self, manager):
        sid = manager.create_session(USER)
        future = (utcnow() + timedelta(minutes=5)).isoformat()
        _set_signed_in_at(manager, sid, "google_1", future)

        assert not manager.signed_in_within(sid, "google_1", timedelta(minutes=10))

    def test_no_time_does_not(self, manager):
        sid = manager.create_session(USER)
        _set_signed_in_at(manager, sid, "google_1", None)

        assert not manager.signed_in_within(sid, "google_1", timedelta(minutes=10))


class TestLinkRouteWithARealSession:
    """The link route against a real SessionManager: the time ``add_account``
    writes is the one the route reads, for the session the cookie names."""

    @staticmethod
    async def _link(manager: SessionManager, sid: str):
        from unittest.mock import AsyncMock, MagicMock, patch

        from api.routes import me_account

        request = MagicMock()
        request.cookies = {me_account.auth_module.SESSION_COOKIE_NAME: sid}
        request.client = None
        request.headers = {}
        service = MagicMock()
        service.link = AsyncMock(return_value=True)
        with (
            patch.object(me_account.auth_module, "_session_manager", manager),
            patch.object(me_account, "IdentityLinkService", return_value=service),
            patch.object(me_account, "schedule_security_notification"),
        ):
            result = await me_account.link_identity(
                me_account.IdentityLinkTarget(user_id="local:admin"),
                request,
                MagicMock(),
                {"user_id": "google_1"},
                AsyncMock(),
            )
        return result, service

    @pytest.mark.asyncio
    async def test_two_fresh_sign_ins_link(self, manager):
        sid = manager.create_session(USER, proven_at=utcnow())
        manager.add_account(sid, OTHER, proven_at=utcnow())

        result, service = await self._link(manager, sid)

        assert result.status == "ok"
        service.link.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale", ["google_1", "local:admin"])
    async def test_a_stale_sign_in_on_either_side_is_refused(self, manager, stale):
        from utils.exceptions import IdentityLinkSignInRequiredError

        sid = manager.create_session(USER, proven_at=utcnow())
        manager.add_account(sid, OTHER, proven_at=utcnow())
        old = (utcnow() - timedelta(minutes=11)).isoformat()
        _set_signed_in_at(manager, sid, stale, old, key="proven_at")

        with pytest.raises(IdentityLinkSignInRequiredError):
            await self._link(manager, sid)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unproven", ["google_1", "local:admin"])
    async def test_a_fresh_sign_in_that_proved_nothing_is_refused(self, manager, unproven):
        """#1818: a sign-in a minute ago is not enough when it proved nothing
        (an OAuth round trip with no provider authentication time)."""
        from utils.exceptions import IdentityLinkSignInRequiredError

        proofs = {"google_1": utcnow(), "local:admin": utcnow()}
        proofs[unproven] = None
        sid = manager.create_session(USER, proven_at=proofs["google_1"])
        manager.add_account(sid, OTHER, proven_at=proofs["local:admin"])
        assert manager.signed_in_within(sid, unproven, timedelta(minutes=10))

        with pytest.raises(IdentityLinkSignInRequiredError):
            await self._link(manager, sid)
