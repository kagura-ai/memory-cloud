"""A password-bearing REDIS_URL reaches both Redis clients intact (#1794).

The single-server compose files let ``REDIS_URL`` come from the env file, so
the API can reach a Redis started with ``REDIS_PASSWORD``; docs/deployment.md
tells operators to percent-encode the password in it. The API builds two
clients from that URL — the asyncio pool in ``db/redis.py`` and the sync
session client in ``auth/session.py`` — and each logs where it points. These
tests pin that both decode the password and that neither log line carries it.
No server is needed: the asyncio pool connects lazily and the session client's
eager PING is stubbed.
"""

import logging
import urllib.parse
from typing import Any

import redis

import auth.session as session_mod
import db.redis as redis_mod

# Every class that breaks a naive URL (@ : / # %), a space and a quote.
PASSWORD = 'k1794 p@ss:w/rd#%x"q'
ENCODED = urllib.parse.quote(PASSWORD, safe="")
URL = f"redis://:{ENCODED}@redis.example:6379/0"


class _PoolSettings:
    redis_max_connections = 5
    redis_pool_timeout_seconds = 2.0


class _CapturingLogger:
    """Stands in for the structlog logger of db/redis.py."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))


def test_the_asyncio_pool_decodes_the_password(monkeypatch):
    log = _CapturingLogger()
    monkeypatch.setattr(redis_mod, "_redis_client", None)
    monkeypatch.setattr(redis_mod, "REDIS_URL", URL)
    monkeypatch.setattr(redis_mod, "get_settings", _PoolSettings)
    monkeypatch.setattr(redis_mod, "logger", log)

    client = redis_mod.get_redis_client()

    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["password"] == PASSWORD
    assert kwargs["host"] == "redis.example"
    assert kwargs["port"] == 6379
    assert log.calls, "get_redis_client logs where it points"
    logged = repr(log.calls)
    assert PASSWORD not in logged
    assert ENCODED not in logged


def test_the_session_client_decodes_the_password(monkeypatch, caplog):
    monkeypatch.setattr(session_mod, "_redis_client_cache", {})
    monkeypatch.setattr(redis.Redis, "ping", lambda self, **kwargs: True)

    with caplog.at_level(logging.DEBUG, logger=session_mod.__name__):
        manager = session_mod.SessionManager(redis_url=URL)

    kwargs = manager._redis.connection_pool.connection_kwargs
    assert kwargs["password"] == PASSWORD
    assert kwargs["host"] == "redis.example"
    assert kwargs["port"] == 6379
    assert caplog.records, "SessionManager logs where it points"
    assert PASSWORD not in caplog.text
    assert ENCODED not in caplog.text
