"""SessionManager log lines never carry REDIS_URL credentials (#1898).

A Redis URL can hold the password in its userinfo (``redis://:pw@host``) or
in its query (``?password=pw``, accepted by redis-py). The three log sites —
"Initialized SessionManager", "Creating new Redis client" and "Reusing cached
Redis client" — print the parsed host and port only.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import auth.session as session_module
from auth.session import SessionManager

_SECRET = "s3cret-pw"
_URLS = [
    f"redis://:{_SECRET}@store:6379/0",
    f"redis://store:6379/0?password={_SECRET}",
]


@pytest.fixture
def empty_client_cache():
    """Run against an empty client cache, and put the real one back after."""
    with patch.dict(session_module._redis_client_cache, clear=True):
        yield


@pytest.mark.parametrize("redis_url", _URLS)
def test_session_manager_logs_hide_the_redis_password(redis_url, caplog, empty_client_cache):
    caplog.set_level(logging.DEBUG, logger="auth.session")

    with patch("redis.Redis.from_url", return_value=MagicMock()) as from_url:
        # First instance creates the client, second one reuses the cached one.
        SessionManager(redis_url=redis_url)
        SessionManager(redis_url=redis_url)

    # The client itself still gets the full URL.
    from_url.assert_called_once()
    assert from_url.call_args.args[0] == redis_url

    messages = [record.getMessage() for record in caplog.records]
    assert any("Creating new Redis client for sessions: store:6379" in m for m in messages)
    assert any("Reusing cached Redis client for store:6379" in m for m in messages)
    assert sum("redis=store:6379)" in m for m in messages) == 2

    assert _SECRET not in caplog.text
    assert "password" not in caplog.text
    assert "@" not in caplog.text
