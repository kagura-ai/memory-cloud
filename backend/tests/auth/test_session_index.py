"""Per-user session index (#1809).

``delete_user_sessions`` used to SCAN every ``session:*`` key to find one
user's sessions, and ``get_active_sessions_count`` ran ``KEYS session:*``.
Each account now has a Redis set of its session ids (``user_sessions:<id>``),
so invalidation reads only that user's sessions.

Sessions written before the index existed have no entry. They stay reachable
two ways: the sweep keeps SCANning for one session lifetime (plus a margin)
after the index first appears, and reading a session indexes it, so a session
that outlives that window is one that was used, and therefore indexed. The
window is measured from a marker the first sweep writes.

A real (fake) Redis is used, not mocks, so the set and TTL semantics are real.
"""

from __future__ import annotations

import json
import time

import fakeredis
import pytest

from auth import session as session_module
from auth.session import SessionManager

TTL = 3600
U1 = {"sub": "u1", "user_id": "u1", "email": "u1@example.com", "role": "user"}
U2 = {"sub": "u2", "user_id": "u2", "email": "u2@example.com", "role": "user"}


@pytest.fixture
def redis(monkeypatch) -> fakeredis.FakeRedis:
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    return fake


@pytest.fixture
def manager(redis) -> SessionManager:
    return SessionManager(redis_url="redis://fake:6379", session_ttl=TTL)


def _index(redis, user_id: str) -> set[str]:
    return set(redis.smembers(f"user_sessions:{user_id}"))


def _plant_unindexed(redis, session_id: str, record: dict) -> None:
    """A session written by the code before #1809: no index entry."""
    redis.setex(f"session:{session_id}", TTL, json.dumps(record))


def _container(identity: dict) -> dict:
    uid = identity["user_id"]
    return {
        "v": 2,
        "accounts": {uid: identity},
        "active": uid,
        "created_at": "2026-01-01T00:00:00",
        "last_accessed": "2026-01-01T00:00:00",
    }


def _window_over(redis) -> None:
    """Pretend the index has existed for longer than the legacy window."""
    redis.set("session_index:since", str(time.time() - 10 * TTL - 10 * 86400))


class _NoScan:
    """Fail the test if the sweep falls back to a keyspace walk."""

    def __init__(self, redis) -> None:
        self.calls = 0
        self._scan = redis.scan
        redis.scan = self._count  # type: ignore[method-assign]

    def _count(self, *args, **kwargs):
        self.calls += 1
        return self._scan(*args, **kwargs)


class TestIndexIsMaintained:
    def test_create_session_indexes_the_user(self, manager, redis):
        sid = manager.create_session(U1)
        assert _index(redis, "u1") == {sid}
        # The index lives at least as long as the session it points at.
        assert redis.ttl("user_sessions:u1") >= redis.ttl(f"session:{sid}") - 1

    def test_the_index_is_not_a_session_key(self, manager, redis):
        manager.create_session(U1)
        # A sweep or count over session:* must never pick the set up.
        assert not any(k.startswith("session:") for k in redis.keys("user_sessions:*"))

    def test_add_account_indexes_the_added_account(self, manager, redis):
        sid = manager.create_session(U1)
        assert manager.add_account(sid, U2)
        assert _index(redis, "u2") == {sid}
        assert _index(redis, "u1") == {sid}

    def test_remove_account_unindexes_it(self, manager, redis):
        sid = manager.create_session(U1)
        manager.add_account(sid, U2)
        assert manager.remove_account(sid, "u2")
        assert _index(redis, "u2") == set()
        assert _index(redis, "u1") == {sid}

    def test_reading_a_pre_index_session_indexes_it(self, manager, redis):
        _plant_unindexed(redis, "old", _container(U1))
        assert manager.get_session("old") is not None
        assert _index(redis, "u1") == {"old"}

    def test_reading_a_legacy_flat_session_indexes_it(self, manager, redis):
        _plant_unindexed(redis, "flat", {**U1, "created_at": "2026-01-01T00:00:00"})
        assert manager.get_session("flat") is not None
        assert _index(redis, "u1") == {"flat"}

    def test_reading_refreshes_the_index_ttl(self, manager, redis):
        sid = manager.create_session(U1)
        redis.expire("user_sessions:u1", 5)
        manager.get_session(sid)
        assert redis.ttl("user_sessions:u1") > 5


class TestSweepUsesTheIndex:
    def test_deletes_the_users_sessions_and_only_those(self, manager, redis):
        _window_over(redis)
        a = manager.create_session(U1)
        b = manager.create_session(U1)
        other = manager.create_session(U2)
        no_scan = _NoScan(redis)

        assert manager.delete_user_sessions("u1", strict=True) == 2

        assert no_scan.calls == 0
        assert redis.get(f"session:{a}") is None
        assert redis.get(f"session:{b}") is None
        assert redis.get(f"session:{other}") is not None
        assert _index(redis, "u1") == set()
        assert _index(redis, "u2") == {other}

    def test_exclusion_is_kept_in_the_index(self, manager, redis):
        _window_over(redis)
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)
        assert manager.delete_user_sessions("u1", exclude_session_id=keep) == 1
        assert redis.get(f"session:{keep}") is not None
        assert redis.get(f"session:{drop}") is None
        assert _index(redis, "u1") == {keep}

    def test_stale_ids_are_pruned(self, manager, redis):
        _window_over(redis)
        sid = manager.create_session(U1)
        redis.delete(f"session:{sid}")  # expired, or a plain logout
        assert manager.delete_user_sessions("u1") == 0
        assert _index(redis, "u1") == set()

    def test_an_id_whose_session_no_longer_holds_the_user_is_spared(self, manager, redis):
        # The index is a hint, ownership is still checked on the record.
        _window_over(redis)
        sid = manager.create_session(U2)
        redis.sadd("user_sessions:u1", sid)
        assert manager.delete_user_sessions("u1") == 0
        assert redis.get(f"session:{sid}") is not None

    def test_strict_raises_when_the_index_cannot_be_read(self, manager, redis, monkeypatch):
        def boom(*_a, **_k):
            raise ConnectionError("redis down")

        monkeypatch.setattr(redis, "smembers", boom)
        with pytest.raises(ConnectionError):
            manager.delete_user_sessions("u1", strict=True)
        assert manager.delete_user_sessions("u1") == 0


class TestPreIndexSessionsAreNotStranded:
    def test_during_the_window_the_sweep_still_scans(self, manager, redis):
        _plant_unindexed(redis, "old", _container(U1))
        _plant_unindexed(redis, "old-flat", dict(U1))
        assert manager.delete_user_sessions("u1", strict=True) == 2
        assert redis.get("session:old") is None
        assert redis.get("session:old-flat") is None

    def test_the_first_sweep_starts_the_window(self, manager, redis):
        before = time.time()
        manager.delete_user_sessions("u1")
        since = float(redis.get("session_index:since"))
        assert before - 1 <= since <= time.time() + 1
        # A later sweep does not move it.
        redis.set("session_index:since", "123.0")
        manager.delete_user_sessions("u1")
        assert redis.get("session_index:since") == "123.0"

    def test_after_the_window_a_used_pre_index_session_is_still_swept(self, manager, redis):
        _plant_unindexed(redis, "old", _container(U1))
        manager.get_session("old")  # any request on it indexes it
        _window_over(redis)
        no_scan = _NoScan(redis)
        assert manager.delete_user_sessions("u1", strict=True) == 1
        assert no_scan.calls == 0
        assert redis.get("session:old") is None

    def test_an_unreadable_marker_falls_back_to_scanning(self, manager, redis):
        redis.set("session_index:since", "not-a-number")
        _plant_unindexed(redis, "old", _container(U1))
        assert manager.delete_user_sessions("u1") == 1


class TestWritesDoNotResurrectASweptSession:
    """A read-modify-write that straddles a sweep must not bring the session back."""

    def test_mutation_racing_a_sweep(self, manager, redis):
        sid = manager.create_session(U1)

        def sweep_mid_write(container):
            manager.delete_user_sessions("u1", strict=True)
            container["active"] = "u1"

        assert manager._mutate_container(sid, sweep_mid_write) is False
        assert redis.get(f"session:{sid}") is None

    def test_update_racing_a_sweep(self, manager, redis, monkeypatch):
        sid = manager.create_session(U1)
        real_get = redis.get

        def get_then_sweep(key):
            value = real_get(key)
            if key == f"session:{sid}":
                redis.delete(key)
            return value

        monkeypatch.setattr(redis, "get", get_then_sweep)
        assert manager.update_session(sid, {"name": "x"}) is False
        monkeypatch.setattr(redis, "get", real_get)
        assert redis.get(f"session:{sid}") is None

    def test_legacy_upgrade_racing_a_sweep(self, manager, redis, monkeypatch):
        _plant_unindexed(redis, "flat", dict(U1))
        real_get = redis.get

        def get_then_sweep(key):
            value = real_get(key)
            if key == "session:flat":
                redis.delete(key)
            return value

        monkeypatch.setattr(redis, "get", get_then_sweep)
        manager.get_session("flat")
        monkeypatch.setattr(redis, "get", real_get)
        assert redis.get("session:flat") is None


class TestCount:
    def test_counts_sessions_without_keys(self, manager, redis, monkeypatch):
        manager.create_session(U1)
        manager.create_session(U2)

        def no_keys(*_a, **_k):
            raise AssertionError("KEYS blocks Redis; use SCAN")

        monkeypatch.setattr(redis, "keys", no_keys)
        assert manager.get_active_sessions_count() == 2


class TestAccessor:
    def test_the_accessor_lives_beside_the_session_store(self):
        sentinel = object()
        session_module.set_session_manager(sentinel)  # type: ignore[arg-type]
        try:
            assert session_module.get_session_manager() is sentinel
        finally:
            session_module.set_session_manager(None)
        assert session_module.SESSION_COOKIE_NAME == "kagura_session"
