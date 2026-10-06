"""A sweep fenced by the lock its caller holds (#1918).

``delete_user_sessions(..., fence=(lock_key, token))`` lands its deletes in
one WATCH/MULTI transaction on the lock key: only while the key still holds
``token``. A sweep that outlived its lease — a stalled Redis call past the
TTL — therefore cannot delete a session that a newer holder of the lock
created and already answered for.

A real (fake) Redis is used, so WATCH and MULTI are real.
"""

from __future__ import annotations

import fakeredis
import pytest

from auth.session import SessionManager, SweepLeaseLostError
from utils.redis_lock import acquire_lock_sync

TTL = 3600
LOCK = "signin_sweep_lock:u1"
U1 = {"sub": "u1", "user_id": "u1", "email": "u1@example.com", "role": "user"}


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def redis(server, monkeypatch) -> fakeredis.FakeRedis:
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(
        SessionManager, "_get_or_create_redis_client", staticmethod(lambda _url: fake)
    )
    return fake


@pytest.fixture
def manager(redis) -> SessionManager:
    return SessionManager(redis_url="redis://fake:6379", session_ttl=TTL)


@pytest.fixture
def _no_legacy_scan(redis) -> None:
    redis.set("session_index:since", "0")


def _live(redis, *session_ids: str) -> list[bool]:
    return [redis.get(f"session:{sid}") is not None for sid in session_ids]


@pytest.mark.usefixtures("_no_legacy_scan")
class TestSweepFence:
    def test_a_held_lease_sweeps_as_usual(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)
        token = acquire_lock_sync(redis, LOCK, 10)
        assert token is not None

        deleted = manager.delete_user_sessions(
            "u1", exclude_session_id=keep, strict=True, fence=(LOCK, token)
        )

        assert deleted == 1
        assert _live(redis, keep, drop) == [True, False]
        assert set(redis.smembers("user_sessions:u1")) == {keep}
        # The fence reads the lock, it does not release it.
        assert redis.get(LOCK) == token

    def test_a_lease_taken_by_a_newer_holder_deletes_nothing(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        newer = manager.create_session(U1)
        redis.set(LOCK, "newer-holder", ex=10)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(LOCK, "expired-token")
            )

        assert _live(redis, keep, newer) == [True, True]
        assert set(redis.smembers("user_sessions:u1")) == {keep, newer}
        assert redis.get(LOCK) == "newer-holder"

    def test_an_expired_lease_nobody_took_deletes_nothing(self, manager, redis) -> None:
        # The candidates may predate the lease's end; a holder in between
        # could have come and gone. Without the lock the sweep's reads are
        # stale, so it does not write.
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(LOCK, "expired-token")
            )

        assert _live(redis, keep, drop) == [True, True]

    def test_the_lease_is_lost_strict_or_not(self, manager, redis) -> None:
        # A caller that fences cares about the answer: it is never "0 swept".
        manager.create_session(U1)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions("u1", fence=(LOCK, "expired-token"))

    def test_a_lease_lost_between_the_check_and_the_deletes_lands_nothing(
        self, manager, redis, server
    ) -> None:
        # The fence read our token, then the lease expired and a newer
        # holder took the lock before EXEC: the WATCH aborts the transaction.
        other = fakeredis.FakeRedis(server=server, decode_responses=True)
        keep = manager.create_session(U1)
        newer = manager.create_session(U1)
        token = acquire_lock_sync(redis, LOCK, 10)
        assert token is not None
        real_pipeline = redis.pipeline

        def _pipeline(transaction: bool = True):
            pipe = real_pipeline(transaction=transaction)
            real_get = pipe.get

            def _get_then_lose_the_lock(key: str):
                held = real_get(key)
                if key == LOCK:
                    other.set(key, "newer-holder")
                return held

            pipe.get = _get_then_lose_the_lock  # type: ignore[method-assign]
            return pipe

        redis.pipeline = _pipeline  # type: ignore[method-assign]

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(LOCK, token)
            )

        assert _live(redis, keep, newer) == [True, True]
        assert redis.get(LOCK) == "newer-holder"

    def test_without_a_fence_the_sweep_is_unchanged(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)
        redis.set(LOCK, "someone-else", ex=10)

        assert manager.delete_user_sessions("u1", exclude_session_id=keep, strict=True) == 1
        assert _live(redis, keep, drop) == [True, False]
