"""Anonymous OIDC protocol endpoints have their own strict burst protection."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from auth.oidc_limits import check_oidc_rate_limit
from utils.exceptions import RedisError


def _request(path="/oidc/token", host="192.0.2.10", forwarded=None):
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "server": ("identity.example.test", 443),
            "client": (host, 12345) if host else None,
        }
    )


@pytest.fixture
def limiter(monkeypatch):
    import auth.oidc_limits as limits

    counter = AsyncMock(return_value=1)
    monkeypatch.setattr(limits, "increment_counter", counter)
    monkeypatch.setattr(
        limits, "get_settings", lambda: SimpleNamespace(oidc_rate_limit_per_minute=120)
    )
    monkeypatch.setattr(limits, "time", SimpleNamespace(time=lambda: 125.0))
    return counter


async def test_limit_allows_exact_budget_and_refuses_next_request(limiter):
    limiter.side_effect = [120, 121]
    assert await check_oidc_rate_limit(_request()) is None
    response = await check_oidc_rate_limit(_request())
    assert response.status_code == 429
    assert response.headers["retry-after"] == "55"
    assert response.headers["cache-control"] == "no-store"


async def test_counter_keys_are_endpoint_and_trusted_asgi_peer_scoped(limiter):
    request = _request(forwarded="198.51.100.99")
    assert await check_oidc_rate_limit(request) is None
    key = limiter.call_args.args[0]
    assert "192.0.2.10" not in key
    assert "198.51.100.99" not in key
    assert hashlib.sha256(b"192.0.2.10").hexdigest() in key
    assert key.endswith(":2")
    assert ":token:" in key
    assert limiter.call_args.kwargs == {"ttl": 60}
    await check_oidc_rate_limit(_request("/oidc/authorize"))
    assert ":authorize:" in limiter.call_args.args[0]


@pytest.mark.parametrize(
    "path", ["/oidc/jwks", "/oidc/.well-known/openid-configuration", "/api/v1/oauth/token"]
)
async def test_metadata_and_existing_oauth_paths_do_not_consume_this_counter(limiter, path):
    assert await check_oidc_rate_limit(_request(path)) is None
    limiter.assert_not_called()


@pytest.mark.parametrize("failure", [RedisError("unavailable"), RuntimeError("unexpected")])
async def test_counter_failure_refuses_protocol_work(limiter, failure):
    limiter.side_effect = failure
    response = await check_oidc_rate_limit(_request("/oidc/revoke"))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


async def test_unknown_peer_uses_one_shared_bucket_and_body_is_not_read(limiter):
    request = _request(host=None)
    assert await check_oidc_rate_limit(request) is None
    assert hashlib.sha256(b"unknown").hexdigest() in limiter.call_args.args[0]


async def test_userinfo_uses_the_same_anonymous_burst_protection(limiter):
    limiter.return_value = 121
    response = await check_oidc_rate_limit(_request("/oidc/userinfo"))
    assert response.status_code == 429
    assert ":userinfo:" in limiter.call_args.args[0]


async def test_invalid_counter_result_fails_closed(limiter):
    limiter.return_value = None
    response = await check_oidc_rate_limit(_request())
    assert response.status_code == 503
