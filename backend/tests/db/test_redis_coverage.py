"""Coverage tests for db.redis async Redis client wrappers.

Exercises the singleton factory, cache helpers, rate-limit counters,
co-activation read/write/clear, the SCAN-and-delete GDPR helper, and the
deprecated close path. External Redis I/O is replaced with
``fakeredis.aioredis.FakeRedis`` (no network), and a small failing-client
double drives every reachable exception branch.

All tests swap ``db.redis._redis_client`` via monkeypatch and never assert on
global state after teardown. Keys use a unique per-test prefix and are cleaned
up by the in-memory fake (one fresh fake per test).
"""

import json
import uuid

import fakeredis.aioredis as fakeaioredis
import pytest

import db.redis as redis_mod
from db.redis import (
    clear_co_activations,
    clear_user_rate_limits,
    close_redis,
    delete_cache,
    get_all_co_activations,
    get_cache,
    get_co_activation,
    get_redis_client,
    incrby_counter,
    increment_counter,
    set_cache,
    set_co_activation,
)
from utils.exceptions import RedisError


@pytest.fixture
def fake_redis(monkeypatch):
    """Install a fresh in-memory FakeRedis as the module singleton.

    Yields the fake so tests can seed/inspect it directly. Restores the
    original singleton afterward so no global state leaks between tests.
    """
    original = redis_mod._redis_client
    fake = fakeaioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis_client", fake)
    yield fake
    monkeypatch.setattr(redis_mod, "_redis_client", original)


class _BoomRedis:
    """Async Redis double whose every command raises, to hit error branches."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("boom")

    async def _raise(self, *args, **kwargs):
        raise self._exc

    # Commands used by db.redis
    get = _raise
    set = _raise
    setex = _raise
    delete = _raise
    incr = _raise
    incrby = _raise
    expire = _raise
    ttl = _raise

    def scan_iter(self, *args, **kwargs):
        async def _gen():
            raise self._exc
            yield  # pragma: no cover - never reached

        return _gen()


@pytest.fixture
def boom_redis(monkeypatch):
    """Install a failing Redis double as the module singleton."""
    original = redis_mod._redis_client
    boom = _BoomRedis()
    monkeypatch.setattr(redis_mod, "_redis_client", boom)
    yield boom
    monkeypatch.setattr(redis_mod, "_redis_client", original)


def _k(name: str) -> str:
    """Unique key for isolation within a single fake instance."""
    return f"test:{name}:{uuid.uuid4().hex}"


class TestGetRedisClient:
    """get_redis_client singleton factory + error wrapping."""

    def test_returns_installed_singleton(self, fake_redis):
        """When a client already exists it is returned without rebuilding."""
        assert get_redis_client() is fake_redis

    def test_builds_client_when_none(self, monkeypatch):
        """First call with no singleton calls from_url and caches the result."""
        monkeypatch.setattr(redis_mod, "_redis_client", None)
        sentinel = object()
        calls = {}

        def fake_from_url(url, **kwargs):
            calls["url"] = url
            calls["kwargs"] = kwargs
            return sentinel

        monkeypatch.setattr(redis_mod.aioredis, "from_url", fake_from_url)
        try:
            client = get_redis_client()
            assert client is sentinel
            # cached: a second call does not rebuild
            assert get_redis_client() is sentinel
            assert calls["kwargs"]["decode_responses"] is True
            assert calls["kwargs"]["max_connections"] == 10
        finally:
            monkeypatch.setattr(redis_mod, "_redis_client", None)

    def test_wraps_connect_failure_in_redis_error(self, monkeypatch):
        """from_url raising is re-raised as RedisError with chained cause."""
        monkeypatch.setattr(redis_mod, "_redis_client", None)

        def boom_from_url(url, **kwargs):
            raise ConnectionError("no redis")

        monkeypatch.setattr(redis_mod.aioredis, "from_url", boom_from_url)
        try:
            with pytest.raises(RedisError, match="Failed to connect to Redis"):
                get_redis_client()
        finally:
            monkeypatch.setattr(redis_mod, "_redis_client", None)


class TestCloseRedis:
    """Deprecated close_redis path."""

    async def test_close_closes_and_clears_singleton(self, monkeypatch):
        """close_redis awaits .close() and resets the singleton to None."""
        closed = {"called": False}

        class _Closeable:
            async def close(self):
                closed["called"] = True

        monkeypatch.setattr(redis_mod, "_redis_client", _Closeable())
        try:
            await close_redis()
            assert closed["called"] is True
            assert redis_mod._redis_client is None
        finally:
            monkeypatch.setattr(redis_mod, "_redis_client", None)

    async def test_close_noop_when_no_client(self, monkeypatch):
        """With no singleton, close_redis is a no-op (does not raise)."""
        monkeypatch.setattr(redis_mod, "_redis_client", None)
        await close_redis()
        assert redis_mod._redis_client is None


class TestGetCache:
    """get_cache happy path + swallowed-error path."""

    async def test_returns_value_when_present(self, fake_redis):
        key = _k("get")
        await fake_redis.set(key, "hello")
        assert await get_cache(key) == "hello"

    async def test_returns_none_when_missing(self, fake_redis):
        assert await get_cache(_k("absent")) is None

    async def test_returns_none_on_error(self, boom_redis):
        """A backend error is logged and swallowed, returning None."""
        assert await get_cache("any") is None


class TestSetCache:
    """set_cache with and without TTL, plus error wrapping."""

    async def test_set_without_ttl(self, fake_redis):
        key = _k("set")
        await set_cache(key, "v")
        assert await fake_redis.get(key) == "v"
        # no TTL set
        assert await fake_redis.ttl(key) == -1

    async def test_set_with_ttl_uses_setex(self, fake_redis):
        key = _k("setex")
        await set_cache(key, "v", ttl=120)
        assert await fake_redis.get(key) == "v"
        ttl = await fake_redis.ttl(key)
        assert 0 < ttl <= 120

    async def test_set_with_zero_ttl_treated_as_no_ttl(self, fake_redis):
        """ttl=0 is falsy, so the plain set branch runs (no expiration)."""
        key = _k("zerottl")
        await set_cache(key, "v", ttl=0)
        assert await fake_redis.ttl(key) == -1

    async def test_set_error_raises_redis_error(self, boom_redis):
        with pytest.raises(RedisError, match="Failed to set cache"):
            await set_cache("k", "v")


class TestDeleteCache:
    """delete_cache happy path + swallowed-error path."""

    async def test_delete_removes_key(self, fake_redis):
        key = _k("del")
        await fake_redis.set(key, "v")
        await delete_cache(key)
        assert await fake_redis.get(key) is None

    async def test_delete_swallows_error(self, boom_redis):
        """A backend error is logged and swallowed (no raise, returns None)."""
        assert await delete_cache("k") is None


class TestIncrementCounter:
    """increment_counter incr + first-increment TTL behavior."""

    async def test_first_increment_returns_one(self, fake_redis):
        key = _k("cnt")
        assert await increment_counter(key) == 1

    async def test_first_increment_sets_ttl(self, fake_redis):
        key = _k("cntttl")
        await increment_counter(key, ttl=60)
        ttl = await fake_redis.ttl(key)
        assert 0 < ttl <= 60

    async def test_second_increment_does_not_reset_ttl(self, fake_redis):
        """Only count==1 sets the TTL; later increments leave it alone."""
        key = _k("cntmulti")
        await increment_counter(key, ttl=60)
        assert await increment_counter(key, ttl=60) == 2
        # still has the original TTL window
        assert 0 < await fake_redis.ttl(key) <= 60

    async def test_increment_without_ttl_leaves_no_expiry(self, fake_redis):
        key = _k("cnto")
        assert await increment_counter(key) == 1
        assert await fake_redis.ttl(key) == -1

    async def test_increment_error_raises(self, boom_redis):
        with pytest.raises(RedisError, match="Failed to increment counter"):
            await increment_counter("k", ttl=60)


class TestIncrbyCounter:
    """incrby_counter incrby + TTL-only-if-absent behavior."""

    async def test_incrby_returns_sum(self, fake_redis):
        key = _k("incrby")
        assert await incrby_counter(key, 5) == 5
        assert await incrby_counter(key, 3) == 8

    async def test_incrby_sets_ttl_when_absent(self, fake_redis):
        key = _k("incrbyttl")
        await incrby_counter(key, 2, ttl=90)
        ttl = await fake_redis.ttl(key)
        assert 0 < ttl <= 90

    async def test_incrby_keeps_existing_ttl(self, fake_redis):
        """Second call sees remaining>=0 and does NOT re-set the TTL."""
        key = _k("incrbykeep")
        await incrby_counter(key, 1, ttl=300)
        first_ttl = await fake_redis.ttl(key)
        await incrby_counter(key, 1, ttl=5)  # smaller ttl must be ignored
        second_ttl = await fake_redis.ttl(key)
        # The original ~300s TTL is preserved, NOT reset down to ~5 and NOT
        # extended past the first observation.
        assert 5 < second_ttl <= first_ttl

    async def test_incrby_without_ttl_no_expiry(self, fake_redis):
        key = _k("incrbyno")
        await incrby_counter(key, 4)
        assert await fake_redis.ttl(key) == -1

    async def test_incrby_error_raises(self, boom_redis):
        with pytest.raises(RedisError, match="Failed to increment counter"):
            await incrby_counter("k", 1, ttl=10)


class TestCoActivation:
    """get/set co-activation with key normalization and JSON round-trip."""

    async def test_set_then_get_round_trip(self, fake_redis):
        record = {"count": 3, "weight": 0.5}
        await set_co_activation("u1", "nodeA", "nodeB", record)
        got = await get_co_activation("u1", "nodeA", "nodeB")
        assert got == record

    async def test_key_order_normalized(self, fake_redis):
        """Reversed node order maps to the same key (node_1 > node_2 swap)."""
        record = {"count": 7}
        await set_co_activation("u1", "zzz", "aaa", record)
        # Query with the opposite order — must resolve to same normalized key.
        got = await get_co_activation("u1", "aaa", "zzz")
        assert got == record
        # And the stored key uses sorted order.
        assert await fake_redis.get("co_act:u1:aaa:zzz") is not None

    async def test_get_normalizes_reversed_order(self, fake_redis):
        """get_co_activation with node_1 > node_2 swaps to the sorted key."""
        # Store under the sorted key directly.
        await fake_redis.set("co_act:u1:aaa:zzz", json.dumps({"v": 9}))
        # Query with reversed order — swap branch (line 178) must run.
        got = await get_co_activation("u1", "zzz", "aaa")
        assert got == {"v": 9}

    async def test_get_missing_returns_none(self, fake_redis):
        assert await get_co_activation("u1", "x", "y") is None

    async def test_set_co_activation_applies_ttl(self, fake_redis):
        await set_co_activation("u1", "a", "b", {"c": 1}, ttl=100)
        ttl = await fake_redis.ttl("co_act:u1:a:b")
        assert 0 < ttl <= 100


class TestGetAllCoActivations:
    """get_all_co_activations bulk scan + parsing + error fallback."""

    async def test_returns_all_records_for_user(self, fake_redis):
        await set_co_activation("user9", "a", "b", {"n": 1})
        await set_co_activation("user9", "c", "d", {"n": 2})
        result = await get_all_co_activations("user9")
        assert result[("a", "b")] == {"n": 1}
        assert result[("c", "d")] == {"n": 2}
        assert len(result) == 2

    async def test_ignores_keys_with_wrong_segment_count(self, fake_redis):
        """A key under the prefix but with !=4 parts is skipped."""
        await set_co_activation("user10", "a", "b", {"n": 1})
        # malformed key matching the scan pattern but with 5 segments
        await fake_redis.set("co_act:user10:a:b:extra", json.dumps({"bad": 1}))
        result = await get_all_co_activations("user10")
        assert result == {("a", "b"): {"n": 1}}

    async def test_skips_empty_value(self, fake_redis):
        """A matching key whose value is empty string is skipped (falsy)."""
        await fake_redis.set("co_act:user11:a:b", "")
        result = await get_all_co_activations("user11")
        assert result == {}

    async def test_empty_for_unknown_user(self, fake_redis):
        assert await get_all_co_activations("nobody") == {}

    async def test_returns_empty_on_error(self, boom_redis):
        """Scan failure is swallowed and an empty dict is returned."""
        assert await get_all_co_activations("u") == {}


class TestClearCoActivations:
    """clear_co_activations via the SCAN-and-delete helper."""

    async def test_deletes_user_co_activations(self, fake_redis):
        await set_co_activation("uc", "a", "b", {"n": 1})
        await set_co_activation("uc", "c", "d", {"n": 2})
        deleted = await clear_co_activations("uc")
        assert deleted == 2
        assert await get_all_co_activations("uc") == {}

    async def test_zero_when_nothing_to_delete(self, fake_redis):
        assert await clear_co_activations("emptyuser") == 0

    async def test_does_not_touch_other_users(self, fake_redis):
        await set_co_activation("keep", "a", "b", {"n": 1})
        await set_co_activation("drop", "a", "b", {"n": 2})
        assert await clear_co_activations("drop") == 1
        assert await get_co_activation("keep", "a", "b") == {"n": 1}

    async def test_returns_zero_on_error(self, boom_redis):
        assert await clear_co_activations("u") == 0

    async def test_batched_delete_over_500_keys(self, fake_redis):
        """More than 500 matching keys exercises the mid-loop flush branch.

        SCAN-while-deleting can skip keys (cursor slots change underfoot), so
        the reported count is not guaranteed to equal the seeded total in a
        single pass. We assert the flush branch ran (count > 500) and that a
        follow-up clear drains whatever remained.
        """
        user = "bulk"
        for i in range(550):
            await fake_redis.set(f"co_act:{user}:n{i:04d}:z", json.dumps({"i": i}))
        deleted = await clear_co_activations(user)
        # The >=500 mid-loop flush must have fired at least once.
        assert deleted > 500
        # A second sweep removes any keys SCAN skipped; eventually all gone.
        await clear_co_activations(user)
        await clear_co_activations(user)
        assert await get_all_co_activations(user) == {}


class TestClearUserRateLimits:
    """clear_user_rate_limits across both rate_limit and quota namespaces."""

    async def test_clears_both_namespaces(self, fake_redis):
        uid = "ru"
        await fake_redis.set(f"rate_limit:user:{uid}:0001", "5")
        await fake_redis.set(f"rate_limit:user:{uid}:0002", "9")
        await fake_redis.set(f"quota:user:{uid}:read:2026-06-23", "1")
        deleted = await clear_user_rate_limits(uid)
        assert deleted == 3
        assert await fake_redis.get(f"rate_limit:user:{uid}:0001") is None
        assert await fake_redis.get(f"quota:user:{uid}:read:2026-06-23") is None

    async def test_does_not_touch_workspace_quota(self, fake_redis):
        """Workspace-scoped quota keys must survive user erasure."""
        uid = "rw"
        await fake_redis.set(f"rate_limit:user:{uid}:0001", "5")
        await fake_redis.set("quota:ws:workspace42:read:2026-06-23", "7")
        deleted = await clear_user_rate_limits(uid)
        assert deleted == 1
        assert await fake_redis.get("quota:ws:workspace42:read:2026-06-23") == "7"

    async def test_zero_when_no_counters(self, fake_redis):
        assert await clear_user_rate_limits("clean") == 0

    async def test_returns_zero_on_error(self, boom_redis):
        assert await clear_user_rate_limits("u") == 0
