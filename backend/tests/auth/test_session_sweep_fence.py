"""A sweep fenced by a key its caller owns (#1918).

``delete_user_sessions(..., fence=(key, token))`` lands its deletes in one
WATCH/MULTI transaction on ``key``: only while it still holds ``token``. The
password sign-in writes the token of the latest holder of its sweep lock
there, so a sweep that outlived its lease — a stalled Redis call past the
TTL — cannot delete a session that a later sign-in created and already
answered for.

A real (fake) Redis is used, so WATCH and MULTI are real.
"""

from __future__ import annotations

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import WatchError

from auth.session import SessionManager, SweepLeaseLostError

TTL = 3600
FENCE = "signin_sweep_fence:u1"
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
    def test_a_fence_that_still_holds_the_token_sweeps_as_usual(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)
        redis.set(FENCE, "mine", ex=TTL)

        deleted = manager.delete_user_sessions(
            "u1", exclude_session_id=keep, strict=True, fence=(FENCE, "mine")
        )

        assert deleted == 1
        assert _live(redis, keep, drop) == [True, False]
        assert set(redis.smembers("user_sessions:u1")) == {keep}
        # The fence is read, not written.
        assert redis.get(FENCE) == "mine"

    def test_a_fence_taken_by_a_later_holder_deletes_nothing(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        newer = manager.create_session(U1)
        redis.set(FENCE, "newer-holder", ex=TTL)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(FENCE, "mine")
            )

        assert _live(redis, keep, newer) == [True, True]
        assert set(redis.smembers("user_sessions:u1")) == {keep, newer}
        assert redis.get(FENCE) == "newer-holder"

    def test_a_missing_fence_deletes_nothing(self, manager, redis) -> None:
        # The fence outlives any sweep; gone, it says nothing about who has
        # been through, so the sweep does not write.
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(FENCE, "mine")
            )

        assert _live(redis, keep, drop) == [True, True]

    def test_the_fence_is_lost_strict_or_not(self, manager, redis) -> None:
        # A caller that fences cares about the answer: it is never "0 swept".
        manager.create_session(U1)

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions("u1", fence=(FENCE, "mine"))

    def test_a_fence_taken_between_the_check_and_the_deletes_lands_nothing(
        self, manager, redis, server
    ) -> None:
        # The fence read our token, then a later holder wrote its own before
        # EXEC: the WATCH aborts the transaction.
        other = fakeredis.FakeRedis(server=server, decode_responses=True)
        keep = manager.create_session(U1)
        newer = manager.create_session(U1)
        redis.set(FENCE, "mine", ex=TTL)
        real_pipeline = redis.pipeline

        def _pipeline(transaction: bool = True):
            pipe = real_pipeline(transaction=transaction)
            real_get = pipe.get

            def _get_then_lose_the_fence(key: str):
                held = real_get(key)
                if key == FENCE:
                    other.set(key, "newer-holder")
                return held

            pipe.get = _get_then_lose_the_fence  # type: ignore[method-assign]
            return pipe

        redis.pipeline = _pipeline  # type: ignore[method-assign]

        with pytest.raises(SweepLeaseLostError):
            manager.delete_user_sessions(
                "u1", exclude_session_id=keep, strict=True, fence=(FENCE, "mine")
            )

        assert _live(redis, keep, newer) == [True, True]
        assert redis.get(FENCE) == "newer-holder"

    def test_a_connection_failure_at_exec_is_not_a_lost_fence(self, manager, redis) -> None:
        # redis-py reports a connection failure while watching as a
        # WatchError. The deletes may have run: that is the Redis failure
        # it is, not "nothing deleted".
        manager.create_session(U1)
        redis.set(FENCE, "mine", ex=TTL)
        real_pipeline = redis.pipeline

        def _pipeline(transaction: bool = True):
            pipe = real_pipeline(transaction=transaction)

            def _exec_loses_the_connection():
                pipe.reset()
                # redis-py raises it from the failure's handler: the failure
                # is the WatchError's implicit context.
                exc = WatchError("A ConnectionError occurred while watching one or more keys")
                exc.__context__ = RedisConnectionError("Connection closed by server.")
                raise exc

            pipe.execute = _exec_loses_the_connection  # type: ignore[method-assign]
            return pipe

        redis.pipeline = _pipeline  # type: ignore[method-assign]

        with pytest.raises(RedisConnectionError):
            manager.delete_user_sessions("u1", strict=True, fence=(FENCE, "mine"))
        # Not strict: a Redis failure reads as 0 swept, as it always has.
        assert manager.delete_user_sessions("u1", fence=(FENCE, "mine")) == 0

    def test_a_failure_after_the_fence_armed_gives_the_connection_back(
        self, manager, redis
    ) -> None:
        # From the fence on the pipeline holds a watching connection; an
        # error while queueing must not keep it out of the pool.
        manager.create_session(U1)
        redis.set(FENCE, "mine", ex=TTL)
        pipes: list = []
        real_pipeline = redis.pipeline

        def _pipeline(transaction: bool = True):
            pipe = real_pipeline(transaction=transaction)

            def _srem_fails(*_a, **_k):
                raise RuntimeError("queueing failed")

            pipe.srem = _srem_fails  # type: ignore[method-assign]
            pipes.append(pipe)
            return pipe

        redis.pipeline = _pipeline  # type: ignore[method-assign]

        with pytest.raises(RuntimeError):
            manager.delete_user_sessions("u1", strict=True, fence=(FENCE, "mine"))

        (pipe,) = pipes
        assert pipe.watching is False
        assert pipe.connection is None

    def test_without_a_fence_the_sweep_is_unchanged(self, manager, redis) -> None:
        keep = manager.create_session(U1)
        drop = manager.create_session(U1)
        redis.set(FENCE, "someone-else", ex=TTL)

        assert manager.delete_user_sessions("u1", exclude_session_id=keep, strict=True) == 1
        assert _live(redis, keep, drop) == [True, False]
