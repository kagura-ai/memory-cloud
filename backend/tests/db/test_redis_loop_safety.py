"""The process-global async Redis client follows the running event loop (#1995).

A ``redis.asyncio`` client's sockets and its pool's waiters belong to the loop
that first used them. Every ``TestClient(app)`` request without ``with`` runs
on a fresh portal loop, and async tests share the session loop, so one global
client handed between loops failed with ``attached to a different loop`` or
``Event loop is closed``. ``get_redis_client()`` now builds one client per
loop and ``close_redis()`` only awaits ``aclose()`` on the loop that owns the
client. In production a uvicorn worker runs one loop, so it builds once.

No server is needed: redis-py connects lazily, and these tests send no command.
"""

import ast
import asyncio
from pathlib import Path

import fakeredis.aioredis as fakeaioredis
import pytest

import api.main as main_mod
import db.redis as redis_mod
from db.redis import close_redis, get_redis_client


class _PoolSettings:
    redis_max_connections = 5
    redis_pool_timeout_seconds = 2.0


class _ClosingStub:
    """Stands in for a built client; records ``aclose()`` and can make it fail."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.closed = 0
        self._exc = exc

    async def aclose(self) -> None:
        self.closed += 1
        if self._exc is not None:
            raise self._exc


@pytest.fixture
def fresh_singleton(monkeypatch):
    """Start with no client and restore the module state afterwards."""
    monkeypatch.setattr(redis_mod, "_redis_client", None)
    monkeypatch.setattr(redis_mod, "get_settings", _PoolSettings)
    yield
    monkeypatch.setattr(redis_mod, "_redis_client", None)


class _StubFactory:
    """Replaces ``Redis.from_pool``; ``fail_with`` makes the next stubs' ``aclose()`` raise."""

    def __init__(self) -> None:
        self.fail_with: Exception | None = None

    def __call__(self, pool: object) -> _ClosingStub:
        return _ClosingStub(self.fail_with)


@pytest.fixture
def stub_clients(monkeypatch, fresh_singleton):
    """Make ``get_redis_client()`` build ``_ClosingStub`` objects."""
    factory = _StubFactory()
    monkeypatch.setattr(redis_mod.aioredis.Redis, "from_pool", factory)
    return factory


class TestOneClientPerLoop:
    def test_a_client_built_on_one_loop_is_not_reused_on_another(self, fresh_singleton):
        """Two ``asyncio.run`` calls get two clients; within one loop it is cached."""

        async def get_twice():
            first = get_redis_client()
            assert get_redis_client() is first
            return first

        a = asyncio.run(get_twice())
        b = asyncio.run(get_twice())

        assert a is not b
        assert redis_mod._redis_client is b

    async def test_a_client_from_another_loop_is_dropped_not_closed(self, stub_clients):
        """The old loop's client is replaced without awaiting its ``aclose()``."""
        old = await asyncio.to_thread(asyncio.run, _get())

        new = get_redis_client()

        assert new is not old
        assert old.closed == 0
        assert get_redis_client() is new

    def test_a_client_built_outside_a_loop_is_adopted_by_the_first_loop(self, fresh_singleton):
        """Built in sync code, the client binds to the first loop that uses it."""
        built_sync = get_redis_client()

        assert asyncio.run(_get()) is built_sync
        assert asyncio.run(_get()) is not built_sync

    async def test_an_installed_fake_is_returned_unchanged(self, monkeypatch):
        """A monkeypatched fake carries no loop record, so it is never rebuilt."""
        fake = fakeaioredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(redis_mod, "_redis_client", fake)

        assert get_redis_client() is fake
        assert await asyncio.to_thread(asyncio.run, _get()) is fake

    async def test_a_fake_installed_over_a_built_client_is_returned(
        self, monkeypatch, stub_clients
    ):
        """The loop record belongs to the built client, not to a fake put in its place."""
        await asyncio.to_thread(asyncio.run, _get())
        fake = fakeaioredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(redis_mod, "_redis_client", fake)

        assert get_redis_client() is fake


class TestCloseRedisLoopAware:
    async def test_awaits_aclose_on_the_owning_loop(self, stub_clients):
        client = get_redis_client()

        await close_redis()

        assert client.closed == 1
        assert redis_mod._redis_client is None

    async def test_resets_the_singleton_when_aclose_fails(self, stub_clients):
        stub_clients.fail_with = RuntimeError("aclose failed")
        client = get_redis_client()

        with pytest.raises(RuntimeError, match="aclose failed"):
            await close_redis()

        assert client.closed == 1
        assert redis_mod._redis_client is None

    async def test_skips_aclose_for_a_client_from_another_loop(self, stub_clients):
        old = await asyncio.to_thread(asyncio.run, _get())

        await close_redis()

        assert old.closed == 0
        assert redis_mod._redis_client is None

    async def test_closes_an_installed_fake(self, monkeypatch, fresh_singleton):
        """A fake has no loop record: close_redis awaits its ``aclose()`` as before."""
        fake = _ClosingStub()
        monkeypatch.setattr(redis_mod, "_redis_client", fake)

        await close_redis()

        assert fake.closed == 1
        assert redis_mod._redis_client is None


async def _get():
    return get_redis_client()


class TestLifespanClosesRedis:
    def test_shutdown_closes_redis_after_the_scheduler_and_before_the_db(self):
        """Cron jobs use Redis, so the scheduler stops first; close_db() comes last.

        Reads the awaited calls after ``yield`` in ``lifespan()`` from the AST,
        so comments and strings do not count.
        """
        src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
        lifespan = next(
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        )
        yield_line = next(n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield))
        awaited = [
            ast.unparse(node.value.func)
            for node in sorted(
                (n for n in ast.walk(lifespan) if isinstance(n, ast.Await)),
                key=lambda n: (n.lineno, n.col_offset),
            )
            if node.lineno > yield_line and isinstance(node.value, ast.Call)
        ]

        assert awaited.count("_close_redis_on_shutdown") == 1
        scheduler_stop = awaited.index("scheduler_leader.stop")
        redis_close = awaited.index("_close_redis_on_shutdown")
        db_close = awaited.index("close_db")
        assert scheduler_stop < redis_close < db_close

    async def test_a_failing_close_is_logged_and_swallowed(self, monkeypatch):
        events: list[tuple[str, dict]] = []

        async def boom():
            raise RuntimeError("redis gone")

        monkeypatch.setattr(redis_mod, "close_redis", boom)
        monkeypatch.setattr(
            main_mod.logger, "warning", lambda event, **kw: events.append((event, kw))
        )

        await main_mod._close_redis_on_shutdown()

        assert [event for event, _ in events] == ["redis_close_failed"]
        assert events[0][1]["error_type"] == "RuntimeError"

    async def test_a_hanging_close_is_bounded(self, monkeypatch):
        events: list[str] = []

        async def hang():
            await asyncio.sleep(30)

        monkeypatch.setattr(redis_mod, "close_redis", hang)
        monkeypatch.setattr(main_mod, "_REDIS_CLOSE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(main_mod.logger, "warning", lambda event, **kw: events.append(event))

        await asyncio.wait_for(main_mod._close_redis_on_shutdown(), timeout=5)

        assert events == ["redis_close_failed"]

    def test_the_bound_is_five_seconds(self):
        assert main_mod._REDIS_CLOSE_TIMEOUT_SECONDS == 5.0
