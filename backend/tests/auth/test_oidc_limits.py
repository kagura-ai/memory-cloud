"""Anonymous OIDC protocol endpoints have their own strict burst protection."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


async def test_workspace_operations_have_bounded_keys_independent_of_identifiers(limiter):
    for workspace in ("11111111-1111-1111-1111-111111111111", "other-untrusted-path"):
        assert (
            await check_oidc_rate_limit(_request(f"/api/v1/me/workspaces/{workspace}/access"))
            is None
        )
    keys = [call.args[0] for call in limiter.call_args_list]
    assert keys[0] == keys[1]
    assert ":workspace-access:" in keys[0]
    assert "untrusted" not in keys[0]
    await check_oidc_rate_limit(_request("/api/v1/me/workspaces"))
    assert ":workspace-list:" in limiter.call_args.args[0]
    assert limiter.call_args.args[0] != keys[0]


# #2903: over the per-peer budget, a request carrying a credential this server
# issued is counted in its holder's own bucket instead of being refused.

ACCESS = "kmc_oidc_at_" + "A" * 43
PEER_429 = {"error": "temporarily_unavailable", "error_description": "OIDC rate limit exceeded."}


def _holder_key(operation: str, holder: str) -> str:
    return f"oidc_rate_limit:{operation}:holder:{hashlib.sha256(holder.encode()).hexdigest()}:2"


@pytest.fixture
def recognised(monkeypatch):
    """The one lookup the limiter may run, recorded; answers ``client-a`` for ``user-1``."""
    import auth.oidc_limits as limits

    lookup = AsyncMock(return_value=("client-a", "user-1"))
    monkeypatch.setattr(limits, "_first_row", lookup)
    return lookup


async def test_presented_credential_is_not_consulted_within_budget(limiter, recognised):
    from auth.oidc_limits import PresentedCredential

    source = AsyncMock(return_value=PresentedCredential("access_token", ACCESS))
    limiter.side_effect = [120]
    assert await check_oidc_rate_limit(_request("/oidc/userinfo"), source) is None
    source.assert_not_awaited()
    recognised.assert_not_awaited()
    assert limiter.await_count == 1


async def test_over_budget_recognised_credential_is_counted_in_its_holders_bucket(
    limiter, recognised
):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, 1]
    presented = PresentedCredential("access_token", ACCESS)
    assert await check_oidc_rate_limit(_request("/oidc/userinfo"), presented) is None
    recognised.assert_awaited_once()
    peer_key, holder_key = (call.args[0] for call in limiter.call_args_list)
    assert hashlib.sha256(b"192.0.2.10").hexdigest() in peer_key
    assert holder_key == _holder_key("userinfo", "oidc-client:client-a:user:user-1")
    assert "client-a" not in holder_key
    assert "user-1" not in holder_key
    assert ACCESS not in holder_key
    assert limiter.call_args.kwargs == {"ttl": 60}


async def test_over_budget_holder_bucket_refuses_its_holder_with_the_same_429(limiter, recognised):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, 120, 121, 121]
    presented = PresentedCredential("access_token", ACCESS)
    assert await check_oidc_rate_limit(_request("/oidc/userinfo"), presented) is None
    refused = await check_oidc_rate_limit(_request("/oidc/userinfo"), presented)
    assert refused.status_code == 429
    assert json.loads(refused.body) == PEER_429
    assert refused.headers["retry-after"] == "55"
    assert refused.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "value",
    [
        None,  # recognised shape, unknown to the store
        "kmc_oidc_at_short",
        "kmc_oidc_rt_" + "A" * 43,  # another credential kind
        "Bearer " + ACCESS,
    ],
)
async def test_over_budget_unrecognised_credential_gets_the_plain_429(limiter, recognised, value):
    from auth.oidc_limits import PresentedCredential

    recognised.return_value = None
    limiter.return_value = 121
    plain = await check_oidc_rate_limit(_request("/oidc/userinfo"))
    presented = PresentedCredential("access_token", ACCESS if value is None else value)
    response = await check_oidc_rate_limit(_request("/oidc/userinfo"), presented)
    assert (response.status_code, response.body) == (plain.status_code, plain.body)
    assert response.headers.items() == plain.headers.items()
    assert limiter.await_count == 2  # no holder bucket for an unrecognised credential
    if value is None:
        recognised.assert_awaited_once()
    else:
        recognised.assert_not_awaited()  # wrong shape never reaches the database


@pytest.mark.parametrize("failure", [RedisError("unavailable"), RuntimeError("unexpected"), None])
async def test_over_budget_holder_counter_failure_fails_closed(limiter, recognised, failure):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, failure]
    presented = PresentedCredential("access_token", ACCESS)
    response = await check_oidc_rate_limit(_request("/oidc/userinfo"), presented)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


async def test_peer_counter_failure_answers_503_before_any_recognition(limiter, recognised):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = RedisError("unavailable")
    source = AsyncMock(return_value=PresentedCredential("access_token", ACCESS))
    response = await check_oidc_rate_limit(_request("/oidc/userinfo"), source)
    assert response.status_code == 503
    source.assert_not_awaited()
    recognised.assert_not_awaited()


async def test_recognition_failure_refuses_like_an_unknown_credential_without_logging_it(
    limiter, recognised, monkeypatch
):
    import auth.oidc_limits as limits
    from auth.oidc_limits import PresentedCredential

    log = Mock()
    monkeypatch.setattr(limits, "logger", log)
    recognised.side_effect = RuntimeError("connection failed for " + ACCESS)
    limiter.return_value = 121
    response = await check_oidc_rate_limit(
        _request("/oidc/userinfo"), PresentedCredential("access_token", ACCESS)
    )
    assert response.status_code == 429
    assert json.loads(response.body) == PEER_429
    log.warning.assert_called_once_with(
        "oidc_rate_limit_recognition_unavailable", failure_type="RuntimeError"
    )
    assert ACCESS not in repr(log.mock_calls)
    assert ACCESS not in repr(PresentedCredential("access_token", ACCESS))


async def test_failing_credential_source_refuses_like_an_unknown_credential(limiter, recognised):
    limiter.return_value = 121
    source = AsyncMock(side_effect=RuntimeError("stream consumed"))
    response = await check_oidc_rate_limit(_request(), source)
    assert response.status_code == 429
    recognised.assert_not_awaited()


@pytest.mark.parametrize(
    "kind,value",
    [
        ("authorization_code", "kmc_oidc_code_" + "C" * 43),
        ("refresh_token", "kmc_oidc_rt_" + "R" * 43),
        ("access_token", ACCESS),
    ],
)
async def test_user_bound_credential_is_counted_per_client_and_user(
    limiter, recognised, kind, value
):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, 1, 121, 1]
    assert await check_oidc_rate_limit(_request(), PresentedCredential(kind, value)) is None
    assert limiter.call_args.args[0] == _holder_key("token", "oidc-client:client-a:user:user-1")
    recognised.return_value = ("client-a", "user-2")
    assert await check_oidc_rate_limit(_request(), PresentedCredential(kind, value)) is None
    assert limiter.call_args.args[0] == _holder_key("token", "oidc-client:client-a:user:user-2")


@pytest.mark.parametrize("row", [("client-a",), ("client-a", None), ("client-a", ""), ("", "u")])
async def test_user_bound_credential_without_a_complete_holder_gets_the_plain_429(
    limiter, recognised, row
):
    from auth.oidc_limits import PresentedCredential

    recognised.return_value = row
    limiter.return_value = 121
    response = await check_oidc_rate_limit(
        _request("/oidc/userinfo"), PresentedCredential("access_token", ACCESS)
    )
    assert response.status_code == 429
    assert json.loads(response.body) == PEER_429
    assert limiter.await_count == 1


async def test_signed_in_browser_is_recognised_as_its_user_without_a_lookup(limiter, recognised):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, 1]
    presented = PresentedCredential("session_user", "user-1")
    assert await check_oidc_rate_limit(_request("/oidc/authorize"), presented) is None
    recognised.assert_not_awaited()
    assert limiter.call_args.args[0] == _holder_key("authorize", "user:user-1")


async def test_sync_service_token_is_recognised_as_its_sync_client(limiter, recognised):
    from auth.oidc_limits import PresentedCredential

    limiter.side_effect = [121, 1]
    path = "/api/v1/sync/installations/11111111-1111-1111-1111-111111111111"
    recognised.return_value = ("client-a",)
    presented = PresentedCredential("sync_token", "kmc_sync_at_" + "B" * 43)
    assert await check_oidc_rate_limit(_request(path), presented) is None
    assert limiter.call_args.args[0] == _holder_key("sync-service", "sync-client:client-a")


@pytest.mark.parametrize(
    "form,kind",
    [
        ({"grant_type": "authorization_code", "code": "kmc_oidc_code_" + "C" * 43}, "code"),
        ({"grant_type": "refresh_token", "refresh_token": "kmc_oidc_rt_" + "R" * 43}, "refresh"),
        ({"grant_type": "refresh_token", "code": "kmc_oidc_code_" + "C" * 43}, None),
        ({"grant_type": "client_credentials", "client_assertion": "x.y.z"}, None),
        ({"grant_type": "authorization_code"}, None),
        (None, None),
    ],
)
def test_token_requests_present_only_the_credential_of_their_grant_type(form, kind):
    from auth.oidc_limits import grant_credential

    credential = grant_credential(form)
    if kind is None:
        assert credential is None
    else:
        assert (
            credential is not None
            and credential.kind
            == {
                "code": "authorization_code",
                "refresh": "refresh_token",
            }[kind]
        )


@pytest.mark.parametrize(
    "token,kind",
    [
        ("kmc_oidc_at_" + "A" * 43, "access_token"),
        ("kmc_oidc_rt_" + "R" * 43, "refresh_token"),
        ("kmc_oidc_code_" + "C" * 43, None),
        ("opaque", None),
    ],
)
def test_revocation_presents_the_token_by_its_kind(token, kind):
    from auth.oidc_limits import revocation_credential

    credential = revocation_credential({"token": token})
    assert (credential.kind if credential else None) == kind
    assert revocation_credential(None) is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-authorizations",
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-authorizations/"
        "22222222-2222-2222-2222-222222222222",
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-installations",
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-installations/"
        "22222222-2222-2222-2222-222222222222/records",
    ],
)
def test_sync_control_routes_are_counted_in_the_sync_human_bucket(path):
    from auth.oidc_limits import rate_limit_operation

    assert rate_limit_operation(path) == "sync-human"


def test_other_workspace_paths_keep_their_buckets():
    from auth.oidc_limits import rate_limit_operation

    workspace = "/api/v1/workspaces/11111111-1111-1111-1111-111111111111"
    assert rate_limit_operation(workspace + "/sync-billing-handoffs") == "sync-setup-handoff"
    assert rate_limit_operation(workspace + "/members") is None
    assert rate_limit_operation(workspace) is None


def _api_routes():
    """``(route, full path)`` for every REST route, read back from the included routers."""
    from api.main import app

    def inner(routes, prefix):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from inner(
                    original.routes, prefix + (getattr(route.include_context, "prefix", "") or "")
                )
                continue
            if getattr(route, "methods", None) and getattr(route, "dependant", None) is not None:
                yield route, prefix + route.path

    yield from inner(app.routes, "")


def _calls(dependant):
    yield dependant.call
    for dependency in dependant.dependencies:
        yield from _calls(dependency)


def test_every_route_behind_an_oidc_or_sync_bearer_dependency_has_a_bucket():
    """A future route that takes these credentials cannot be left uncounted."""
    import re

    from auth.oidc_bearer import identity_access_token
    from auth.oidc_limits import rate_limit_operation
    from auth.sync_bearer import sync_service_token

    guarded = {identity_access_token, sync_service_token}
    checked = []
    for route, path in _api_routes():
        if guarded & set(_calls(route.dependant)):
            concrete = re.sub(r"\{[^}]+\}", "11111111-1111-1111-1111-111111111111", path)
            assert rate_limit_operation(concrete) is not None, (sorted(route.methods), path)
            checked.append((frozenset(route.methods), path))
    pattern = "/api/v1/workspaces/{workspace_id}/"
    for method, suffix in (
        ("POST", "sync-authorizations"),
        ("DELETE", "sync-authorizations/{authorization_id}"),
        ("POST", "sync-installations"),
        ("POST", "sync-billing-handoffs"),
    ):
        assert any(method in methods and path == pattern + suffix for methods, path in checked)
    assert any(path.startswith("/api/v1/sync/installations/") for _, path in checked)
    assert any(path.startswith("/api/v1/me/workspaces") for _, path in checked)
    for path in ("/oidc/authorize", "/oidc/token", "/oidc/revoke", "/oidc/userinfo"):
        assert rate_limit_operation(path) == path.rsplit("/", 1)[-1]
