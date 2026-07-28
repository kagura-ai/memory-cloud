"""Tests for the Slack connector channel-list endpoint (#1391).

Covers the ``GET /workspace-connectors/{connector_id}/channels`` handler and the
underlying ``services.slack_channels`` proxy. The Slack HTTP call is mocked at
the ``httpx.AsyncClient`` layer so the real error-mapping path is exercised.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from api.routes.workspace_connectors import list_connector_channels
from utils.exceptions import ConnectorScopeError, ExternalServiceError, NotFoundException


class _FakeRedis:
    """Minimal async Redis stub: setex / get over a dict."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


def _slack_response(*, status_code=200, json_body=None, headers=None):
    """Build a mock httpx.Response-like object for conversations.list."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body or {}

    def _raise_for_status():
        if status_code >= 400:
            raise httpx.HTTPStatusError("err", request=MagicMock(), response=resp)

    resp.raise_for_status = MagicMock(side_effect=_raise_for_status)
    return resp


def _http_ctx(resp):
    """Wrap a mock response in an async-context-manager httpx client."""
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=http_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, http_client


def _connector(*, connector_type="slack", bot_token="xoxb-secret-123"):
    connector = MagicMock()
    connector.connector_type = connector_type
    connector.get_oauth_tokens.return_value = (
        {"bot_token": bot_token} if bot_token is not None else None
    )
    return connector


def _admin():
    return {"user_id": "u1", "current_workspace_id": uuid4()}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_channels_and_next_cursor():
    """Happy path: minimized channels + Slack's pagination cursor pass through."""
    admin = _admin()
    connector_id = uuid4()
    resp = _slack_response(
        json_body={
            "ok": True,
            "channels": [
                {"id": "C01", "name": "general", "is_private": False, "num_members": 42},
                {"id": "C02", "name": "random", "is_private": False, "topic": "secret"},
            ],
            "response_metadata": {"next_cursor": "CURSOR2"},
        }
    )
    ctx, _ = _http_ctx(resp)
    redis = _FakeRedis()

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=redis),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        result = await list_connector_channels(
            connector_id, admin, cursor=None, q=None, db=MagicMock()
        )

    assert result.next_cursor == "CURSOR2"
    assert [(c.id, c.name, c.is_private) for c in result.channels] == [
        ("C01", "general", False),
        ("C02", "random", False),
    ]
    # Minimization: no member counts / topics on the response.
    dumped = result.model_dump_json()
    assert "num_members" not in dumped
    assert "secret" not in dumped
    # The fetched page was cached (minimized, no token).
    cached_key = f"slack_channels:{connector_id}:"
    assert cached_key in redis.store
    assert "xoxb" not in redis.store[cached_key]


@pytest.mark.asyncio
async def test_q_filters_fetched_page_case_insensitively():
    admin = _admin()
    resp = _slack_response(
        json_body={
            "ok": True,
            "channels": [
                {"id": "C01", "name": "General", "is_private": False},
                {"id": "C02", "name": "random", "is_private": False},
                {"id": "C03", "name": "eng-general", "is_private": False},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    )
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=_FakeRedis()),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        result = await list_connector_channels(
            uuid4(), admin, cursor=None, q="GENERAL", db=MagicMock()
        )

    assert {c.id for c in result.channels} == {"C01", "C03"}
    # Empty Slack cursor normalizes to None.
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_cache_hit_skips_slack_call_and_applies_q():
    """A warm cache entry serves without an outbound Slack request; q still filters."""
    admin = _admin()
    connector_id = uuid4()
    cached = json.dumps(
        {
            "channels": [
                {"id": "C01", "name": "general", "is_private": False, "is_member": True},
                {"id": "C02", "name": "random", "is_private": False, "is_member": False},
            ],
            "next_cursor": "NEXT",
        }
    )
    ctx, http_client = _http_ctx(_slack_response(json_body={"ok": True}))

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch(
            "api.routes.workspace_connectors.get_cache",
            AsyncMock(return_value=cached),
        ),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=_FakeRedis()),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        result = await list_connector_channels(
            connector_id, admin, cursor=None, q="random", db=MagicMock()
        )

    http_client.get.assert_not_awaited()  # served from cache
    assert [c.id for c in result.channels] == ["C02"]
    assert result.next_cursor == "NEXT"


@pytest.mark.asyncio
async def test_pre_1451_cache_entry_is_refetched_not_defaulted():
    """#1451: entries cached before ``is_member`` existed must miss, not default.

    ``SlackChannel`` has no default for ``is_member`` precisely so a legacy
    payload raises on rehydrate and falls through to a live fetch. Defaulting it
    to False would paint every already-joined channel as "bot not in channel" —
    a wrong warning is worse than the silence this issue set out to fix.
    """
    admin = _admin()
    connector_id = uuid4()
    legacy_cached = json.dumps(
        {
            "channels": [{"id": "C01", "name": "general", "is_private": False}],
            "next_cursor": None,
        }
    )
    resp = _slack_response(
        json_body={
            "ok": True,
            "channels": [{"id": "C01", "name": "general", "is_private": False, "is_member": True}],
            "response_metadata": {"next_cursor": ""},
        }
    )
    ctx, http_client = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch(
            "api.routes.workspace_connectors.get_cache",
            AsyncMock(return_value=legacy_cached),
        ),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=_FakeRedis()),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        result = await list_connector_channels(
            connector_id, admin, cursor=None, q=None, db=MagicMock()
        )

    http_client.get.assert_awaited_once()  # legacy entry treated as a miss
    assert [(c.id, c.is_member) for c in result.channels] == [("C01", True)]


@pytest.mark.asyncio
async def test_bot_membership_passes_through_from_slack():
    """#1451: ``is_member`` reaches the picker verbatim — Slack does not deliver
    message events for channels the bot has not joined, so a selection there
    ingests nothing and reports nothing."""
    admin = _admin()
    resp = _slack_response(
        json_body={
            "ok": True,
            "channels": [
                {"id": "C01", "name": "joined", "is_private": False, "is_member": True},
                {"id": "C02", "name": "not-joined", "is_private": False, "is_member": False},
                # Absent (older Slack payloads / odd shapes) → treated as not a
                # member, the conservative reading: we cannot prove delivery.
                {"id": "C03", "name": "unknown", "is_private": False},
            ],
            "response_metadata": {"next_cursor": ""},
        }
    )
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=_FakeRedis()),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        result = await list_connector_channels(
            connector_id=uuid4(), admin=admin, cursor=None, q=None, db=MagicMock()
        )

    assert [(c.id, c.is_member) for c in result.channels] == [
        ("C01", True),
        ("C02", False),
        ("C03", False),
    ]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_scope_maps_to_409_connector_scope():
    admin = _admin()
    resp = _slack_response(json_body={"ok": False, "error": "missing_scope"})
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        with pytest.raises(ConnectorScopeError) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 409
    assert exc.value.error_code == "CONNECTOR-SCOPE"


@pytest.mark.asyncio
async def test_rate_limited_http_429_maps_to_429_with_retry_after():
    from fastapi import HTTPException

    admin = _admin()
    resp = _slack_response(
        status_code=429,
        headers={"Retry-After": "30"},
        json_body={"ok": False, "error": "ratelimited"},
    )
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        with pytest.raises(HTTPException) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "30"}


@pytest.mark.asyncio
async def test_rate_limited_body_error_maps_to_429():
    from fastapi import HTTPException

    admin = _admin()
    resp = _slack_response(json_body={"ok": False, "error": "rate_limited"})
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        with pytest.raises(HTTPException) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 429
    # No header when Slack sent the limit as a body error only.
    assert exc.value.headers in (None, {})


@pytest.mark.asyncio
async def test_other_slack_error_maps_to_structured_502_not_raw_5xx():
    admin = _admin()
    resp = _slack_response(json_body={"ok": False, "error": "invalid_auth"})
    ctx, _ = _http_ctx(resp)

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector())
        with pytest.raises(ExternalServiceError) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Authorization / lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_connector_is_404():
    """An unknown / cross-workspace connector returns a uniform 404 (no Slack call)."""
    admin = _admin()

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient") as http_cls,
    ):
        svc.return_value.get_connector = AsyncMock(return_value=None)
        with pytest.raises(NotFoundException) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 404
    assert exc.value.message == "Connector not found"
    http_cls.assert_not_called()


@pytest.mark.asyncio
async def test_missing_workspace_is_400():
    from utils.exceptions import BadRequestError

    admin = {"user_id": "u1", "current_workspace_id": None}
    with pytest.raises(BadRequestError) as exc:
        await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_non_slack_connector_is_400():
    from utils.exceptions import BadRequestError

    admin = _admin()
    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
    ):
        svc.return_value.get_connector = AsyncMock(
            return_value=_connector(connector_type="discord")
        )
        with pytest.raises(BadRequestError) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 400
    assert exc.value.error_code == "CONNECTOR-CHANNELS-001"


@pytest.mark.asyncio
async def test_connector_without_bot_token_maps_to_409_scope():
    admin = _admin()
    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector(bot_token=None))
        with pytest.raises(ConnectorScopeError) as exc:
            await list_connector_channels(uuid4(), admin, cursor=None, q=None, db=MagicMock())

    assert exc.value.status_code == 409
    assert exc.value.error_code == "CONNECTOR-SCOPE"


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_token_never_logged_or_returned():
    """The bot token appears only in the outbound Authorization header — never in
    logs, the cached payload, or the response body."""
    admin = _admin()
    connector_id = uuid4()
    token = "xoxb-super-secret-token-value"
    resp = _slack_response(
        json_body={
            "ok": True,
            "channels": [{"id": "C01", "name": "general", "is_private": False}],
            "response_metadata": {"next_cursor": ""},
        }
    )
    ctx, http_client = _http_ctx(resp)
    redis = _FakeRedis()

    logged: list[str] = []

    def _capture(event, **kw):
        logged.append(event + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

    with (
        patch("api.routes.workspace_connectors.ConnectorProvisioningService") as svc,
        patch("services.slack_channels.httpx.AsyncClient", return_value=ctx),
        patch("api.routes.workspace_connectors.get_cache", AsyncMock(return_value=None)),
        patch("api.routes.workspace_connectors.get_redis_client", return_value=redis),
        patch("api.routes.workspace_connectors.logger.info", side_effect=_capture),
        patch("api.routes.workspace_connectors.logger.warning", side_effect=_capture),
        patch("services.slack_channels.logger.info", side_effect=_capture),
        patch("services.slack_channels.logger.warning", side_effect=_capture),
    ):
        svc.return_value.get_connector = AsyncMock(return_value=_connector(bot_token=token))
        result = await list_connector_channels(
            connector_id, admin, cursor=None, q=None, db=MagicMock()
        )

    # Token was sent only via the Authorization header.
    _, kwargs = http_client.get.call_args
    assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
    # Never in logs.
    assert all(token not in line for line in logged)
    # Never in the response.
    assert token not in result.model_dump_json()
    # Never in the cache.
    assert all(token not in v for v in redis.store.values())
