"""The process-global async Qdrant client follows the running event loop (#2241).

Same binding as the Redis client (#1995): the async client's HTTP connections
belong to the loop that first used them, and a pytest process runs several
loops (the pytest-asyncio session loop, one per ``TestClient`` portal).
``get_qdrant_client()`` builds one client per loop; a uvicorn worker runs one
loop, so production builds once. The constructor is stubbed: no server needed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config.settings as settings_mod
import db.qdrant as qmod
from db.qdrant import get_qdrant_client


class _Client:
    """Stands in for ``AsyncQdrantClient``; accepts attributes like the real one."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


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
