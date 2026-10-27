"""The process-global async Qdrant client follows the running event loop (#2241).

Same binding as the Redis client (#1995): the async client's HTTP connections
belong to the loop that first used them, and a pytest process runs several
loops (the pytest-asyncio session loop, one per ``TestClient`` portal).
``get_qdrant_client()`` builds one client per loop; a uvicorn worker runs one
loop, so production builds once; the lifespan closes it at shutdown. The
constructor is stubbed: no server needed.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import api.main as main_mod
import config.settings as settings_mod
import db.qdrant as qmod
from db.qdrant import close_qdrant, get_qdrant_client


class _Client:
    """Stands in for ``AsyncQdrantClient``; accepts attributes like the real one."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = 0
        self.fail_with: Exception | None = None

    async def close(self) -> None:
        self.closed += 1
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture
def stub_ctor(monkeypatch):
    """No cached client; ``get_qdrant_client()`` builds ``_Client`` objects."""
    built: list[_Client] = []

    def ctor(**kwargs):
        client = _Client(**kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(qmod, "_qdrant_client", None)
    monkeypatch.setattr(qmod, "AsyncQdrantClient", ctor)
    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: SimpleNamespace(qdrant_api_key=None, qdrant_http_compression=False),
    )
    return built


async def _get_twice():
    first = get_qdrant_client()
    assert get_qdrant_client() is first
    return first


def test_a_client_built_on_one_loop_is_not_reused_on_another(stub_ctor):
    """Two ``asyncio.run`` calls get two clients; within one loop it is cached."""
    a = asyncio.run(_get_twice())
    b = asyncio.run(_get_twice())

    assert a is not b
    assert stub_ctor == [a, b]


def test_a_client_built_outside_a_loop_is_adopted_by_the_first_loop(stub_ctor):
    outside = get_qdrant_client()

    assert asyncio.run(_get_twice()) is outside
    # Now owned by that (closed) loop: the next loop builds its own.
    assert asyncio.run(_get_twice()) is not outside
    assert len(stub_ctor) == 2


async def test_an_installed_fake_is_returned_unchanged(monkeypatch):
    """A test's ``MagicMock`` in ``_qdrant_client`` is never rebuilt, on any loop."""
    fake = MagicMock()
    monkeypatch.setattr(qmod, "_qdrant_client", fake)

    assert get_qdrant_client() is fake
    assert await asyncio.to_thread(asyncio.run, _get_twice()) is fake


class TestCloseQdrant:
    async def test_closes_the_client_on_its_own_loop(self, stub_ctor):
        client = get_qdrant_client()

        await close_qdrant()

        assert client.closed == 1
        assert qmod._qdrant_client is None

    async def test_drops_a_client_from_another_loop_without_closing_it(self, stub_ctor):
        other = await asyncio.to_thread(asyncio.run, _get_twice())

        await close_qdrant()

        assert other.closed == 0
        assert qmod._qdrant_client is None

    async def test_forgets_the_client_when_close_fails(self, stub_ctor):
        client = get_qdrant_client()
        client.fail_with = RuntimeError("qdrant gone")

        with pytest.raises(RuntimeError):
            await close_qdrant()

        assert qmod._qdrant_client is None

    async def test_nothing_to_close(self, monkeypatch):
        monkeypatch.setattr(qmod, "_qdrant_client", None)

        await close_qdrant()


class TestLifespanClosesQdrant:
    def test_shutdown_closes_qdrant_after_the_scheduler_and_before_the_db(self):
        """Awaited calls after ``yield`` in ``lifespan()``, read from the AST."""
        src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text(
            encoding="utf-8"
        )
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

        assert awaited.count("_close_qdrant_on_shutdown") == 1
        assert (
            awaited.index("scheduler_leader.stop")
            < awaited.index("_close_qdrant_on_shutdown")
            < awaited.index("close_db")
        )

    async def test_a_failing_close_is_logged_and_swallowed(self, monkeypatch):
        events: list[tuple[str, dict]] = []

        async def boom():
            raise RuntimeError("qdrant gone")

        monkeypatch.setattr(qmod, "close_qdrant", boom)
        monkeypatch.setattr(
            main_mod.logger, "warning", lambda event, **kw: events.append((event, kw))
        )

        await main_mod._close_qdrant_on_shutdown()

        assert [event for event, _ in events] == ["qdrant_close_failed"]
        assert events[0][1]["error_type"] == "RuntimeError"

    async def test_a_hanging_close_is_bounded(self, monkeypatch):
        events: list[str] = []

        async def hang():
            await asyncio.sleep(30)

        monkeypatch.setattr(qmod, "close_qdrant", hang)
        monkeypatch.setattr(main_mod, "_QDRANT_CLOSE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(main_mod.logger, "warning", lambda event, **kw: events.append(event))

        await asyncio.wait_for(main_mod._close_qdrant_on_shutdown(), timeout=5)

        assert events == ["qdrant_close_failed"]
