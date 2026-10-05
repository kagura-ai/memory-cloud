"""The asyncio pool's start-up log line shows host and port only (#1898).

``redis_client_initialized`` used to log the URL with the userinfo password
masked and the rest kept — including a ``?password=`` query, which redis-py
accepts. No server is needed: the pool connects lazily.
"""

from typing import Any

import pytest

import db.redis as redis_mod

_SECRET = "s3cret-pw"


class _PoolSettings:
    redis_max_connections = 5
    redis_pool_timeout_seconds = 2.0


class _CapturingLogger:
    """Stands in for the structlog logger of db/redis.py."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))


@pytest.mark.parametrize(
    "url",
    [
        f"redis://:{_SECRET}@redis.example:6379/0",
        f"redis://redis.example:6379/0?password={_SECRET}",
    ],
)
def test_the_pool_log_line_carries_host_and_port_only(url, monkeypatch):
    log = _CapturingLogger()
    monkeypatch.setattr(redis_mod, "_redis_client", None)
    monkeypatch.setattr(redis_mod, "REDIS_URL", url)
    monkeypatch.setattr(redis_mod, "get_settings", _PoolSettings)
    monkeypatch.setattr(redis_mod, "logger", log)

    client = redis_mod.get_redis_client()

    # The pool itself still gets the password from the full URL.
    assert client.connection_pool.connection_kwargs["password"] == _SECRET
    assert [event for event, _ in log.calls] == ["redis_client_initialized"]
    assert log.calls[0][1]["url"] == "redis.example:6379"
    assert _SECRET not in repr(log.calls)
