"""When each account of a session last proved its credential (#1818).

#1803 made an identity link ask for a recent sign-in of both accounts. A
sign-in is not always a proof: an OAuth round trip completes without a password
while the browser still has a session with the provider. The container
therefore keeps a second time per account, written only from what the caller
says the sign-in proved, and the link reads that one.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from auth.session import SessionManager
from tests.auth.test_session_container import USER, FakeRedis, raw
from utils.datetime import utcnow

OTHER = {"sub": "local:admin", "user_id": "local:admin", "email": "admin@local"}
WINDOW = timedelta(minutes=10)


@pytest.fixture
def manager(monkeypatch) -> SessionManager:
    fake = FakeRedis()
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    return SessionManager(redis_url="redis://fake:6379")


class TestProvenAt:
    def test_a_sign_in_proves_nothing_unless_told(self, manager):
        """Fail closed: the default is no proof, even though it is a sign-in."""
        sid = manager.create_session(USER)

        assert manager.signed_in_within(sid, "google_1", WINDOW)
        assert manager.proven_at(sid, "google_1") is None
        assert not manager.proven_within(sid, "google_1", WINDOW)

    def test_create_session_records_the_given_time(self, manager):
        when = utcnow() - timedelta(minutes=3)
        sid = manager.create_session(USER, proven_at=when)

        assert manager.proven_at(sid, "google_1") == when
        assert manager.proven_within(sid, "google_1", WINDOW)

    def test_an_old_provider_authentication_is_not_recent(self, manager):
        """Google's auth_time from hours ago, delivered by a sign-in just now."""
        sid = manager.create_session(USER, proven_at=utcnow() - timedelta(hours=3))

        assert manager.signed_in_within(sid, "google_1", WINDOW)
        assert not manager.proven_within(sid, "google_1", WINDOW)

    def test_add_account_records_the_added_account_only(self, manager):
        sid = manager.create_session(USER)

        assert manager.add_account(sid, OTHER, proven_at=utcnow())

        assert manager.proven_within(sid, "local:admin", WINDOW)
        assert manager.proven_at(sid, "google_1") is None

    def test_a_sign_in_that_proves_nothing_keeps_an_earlier_proof(self, manager):
        """A proof is a past event; a later sign-in does not undo it."""
        sid = manager.create_session(USER)
        proved = utcnow()
        manager.add_account(sid, OTHER, proven_at=proved)

        assert manager.add_account(sid, OTHER)

        assert manager.proven_at(sid, "local:admin") == proved

    def test_an_older_proof_does_not_replace_a_newer_one(self, manager):
        """A stale Google auth_time must not undo a fresh password proof."""
        sid = manager.create_session(USER)
        fresh = utcnow()
        manager.add_account(sid, OTHER, proven_at=fresh)

        assert manager.add_account(sid, OTHER, proven_at=fresh - timedelta(hours=3))

        assert manager.proven_at(sid, "local:admin") == fresh

    def test_a_newer_proof_replaces_an_older_one(self, manager):
        sid = manager.create_session(USER)
        old = utcnow() - timedelta(hours=3)
        manager.add_account(sid, OTHER, proven_at=old)
        fresh = utcnow()

        assert manager.add_account(sid, OTHER, proven_at=fresh)

        assert manager.proven_at(sid, "local:admin") == fresh

    def test_a_future_time_does_not_count(self, manager):
        sid = manager.create_session(USER, proven_at=utcnow() + timedelta(minutes=5))

        assert not manager.proven_within(sid, "google_1", WINDOW)

    def test_switch_and_update_do_not_touch_it(self, manager):
        sid = manager.create_session(USER, proven_at=utcnow())
        manager.add_account(sid, OTHER)
        before = raw(manager, sid)["proven_at"]

        assert manager.switch_account(sid, "google_1")
        assert manager.update_session(sid, {"proven_at": {"local:admin": utcnow().isoformat()}})

        assert raw(manager, sid)["proven_at"] == before

    def test_removing_an_account_drops_its_proof(self, manager):
        sid = manager.create_session(USER, proven_at=utcnow())
        manager.add_account(sid, OTHER, proven_at=utcnow())

        assert manager.remove_account(sid, "local:admin")

        assert "local:admin" not in raw(manager, sid)["proven_at"]

    def test_a_session_from_before_the_key_has_no_proof(self, manager):
        sid = manager.create_session(USER, proven_at=utcnow())
        stored = raw(manager, sid)
        del stored["proven_at"]
        manager._redis.store[f"session:{sid}"] = json.dumps(stored)

        assert manager.proven_at(sid, "google_1") is None
