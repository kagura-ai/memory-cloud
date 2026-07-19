"""Tests for Slack connector OAuth flow (Spec 2026-06-02, Plan 4)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


class _FakeRedis:
    """Minimal async Redis stub: setex / get / delete over a dict."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _settings(**over):
    base = {
        "slack_client_id": "cid",
        "slack_client_secret": "csec",
        "slack_redirect_uri": "http://localhost:8080/api/v1/connectors/slack/callback",
        "slack_oauth_scopes": "chat:write",
        "frontend_url": "http://localhost:3000/",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _fake_encryptor():
    """Return a minimal encryptor stub: encrypt prepends 'ENC:', decrypt strips it."""
    enc = MagicMock()
    enc.encrypt = lambda s: f"ENC:{s}"
    enc.decrypt = lambda s: s[4:] if s.startswith("ENC:") else s
    return enc


@pytest.mark.asyncio
async def test_install_redirects_to_slack_and_stores_state():
    from api.routes.connectors_slack import slack_install

    redis = _FakeRedis()
    ws_id = uuid4()
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
    ):
        resp = await slack_install(admin)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://slack.com/oauth/v2/authorize?")
    # State persisted → workspace id.
    assert any(k.startswith("slack_oauth_state:") for k in redis.store)
    assert str(ws_id) in redis.store.values()


@pytest.mark.asyncio
async def test_install_503_when_unconfigured():
    from fastapi import HTTPException

    from api.routes.connectors_slack import slack_install

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with patch(
        "api.routes.connectors_slack.get_settings", return_value=_settings(slack_client_id="")
    ):
        with pytest.raises(HTTPException) as exc:
            await slack_install(admin)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_callback_exchanges_code_and_stashes_encrypted_install():
    """Callback requires WorkspaceAdmin; bot_token is Fernet-encrypted in Redis."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {
        "ok": True,
        "access_token": "xoxb-123",
        "team": {"id": "T01", "name": "Acme"},
        "authed_user": {"id": "U01"},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=token_resp)
    http_ctx = MagicMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.httpx.AsyncClient", return_value=http_ctx),
        patch("api.routes.connectors_slack.get_encryptor", return_value=_fake_encryptor()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    assert "/workspace/integrations/connectors?slack_install=" in resp.headers["location"]
    # State consumed.
    assert "slack_oauth_state:st" not in redis.store
    # Install stashed with encrypted bot_token (no plaintext bot_token key).
    install_raw = next(v for k, v in redis.store.items() if k.startswith("slack_install:"))
    install = json.loads(install_raw)
    assert "bot_token" not in install, "bot_token must not be stored in plaintext"
    assert install["bot_token_enc"] == "ENC:xoxb-123"
    assert install["team_id"] == "T01"
    assert install["installing_admin_user_id"] == "U01"
    assert str(install["workspace_id"]) == str(ws_id)


@pytest.mark.asyncio
async def test_callback_unknown_state_redirects_expired():
    """#1381: expired TTL or replayed (consumed) state → expired redirect, not 400 JSON."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="missing")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=expired")


@pytest.mark.asyncio
async def test_callback_workspace_mismatch_redirects_failed():
    """#1381: workspace mismatch → generic failed redirect (no mismatch oracle in
    the URL); the state stays in Redis so a correct-workspace tab can still finish."""
    from api.routes.connectors_slack import slack_callback

    stored_ws = uuid4()
    different_ws = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(stored_ws)})
    admin = {"user_id": "u1", "current_workspace_id": different_ws}  # mismatch

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")
    # Pre-#1381 behavior pin: the mismatch lane does NOT consume the state.
    assert "slack_oauth_state:st" in redis.store


@pytest.mark.asyncio
async def test_callback_state_read_failure_redirects_failed():
    """#1381: Redis outage while reading the state → failed redirect, not 503 JSON."""
    from api.routes.connectors_slack import slack_callback

    class _BrokenRedis(_FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis down")

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_BrokenRedis()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
async def test_callback_exchange_failure_redirects_failed():
    """#1381: Slack token-exchange failure → failed redirect; raw error not reflected."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {"ok": False, "error": "invalid_grant_secret_detail"}
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=token_resp)
    http_ctx = MagicMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.httpx.AsyncClient", return_value=http_ctx),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.endswith("?slack_error=failed")
    assert "invalid_grant_secret_detail" not in location
    # State consumed (one-time use) before the exchange.
    assert "slack_oauth_state:st" not in redis.store


@pytest.mark.asyncio
async def test_callback_encrypt_failure_redirects_failed():
    """#1381: Fernet failure while securing the bot token → failed redirect."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {
        "ok": True,
        "access_token": "xoxb-123",
        "team": {"id": "T01", "name": "Acme"},
        "authed_user": {"id": "U01"},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=token_resp)
    http_ctx = MagicMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)

    broken_encryptor = MagicMock()
    broken_encryptor.encrypt.side_effect = RuntimeError("kms down")

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.httpx.AsyncClient", return_value=http_ctx),
        patch("api.routes.connectors_slack.get_encryptor", return_value=broken_encryptor),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
async def test_callback_install_store_failure_redirects_failed():
    """#1381: Redis outage while stashing the install bundle → failed redirect."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()

    class _SetexBrokenRedis(_FakeRedis):
        async def setex(self, key, ttl, value):
            raise ConnectionError("redis down")

    redis = _SetexBrokenRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {
        "ok": True,
        "access_token": "xoxb-123",
        "team": {"id": "T01", "name": "Acme"},
        "authed_user": {"id": "U01"},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=token_resp)
    http_ctx = MagicMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.httpx.AsyncClient", return_value=http_ctx),
        patch("api.routes.connectors_slack.get_encryptor", return_value=_fake_encryptor()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
async def test_callback_cancel_redirects_with_cancelled():
    """#1375: user denies the Slack consent → 303 to connectors page, not raw 422."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
    ):
        resp = await slack_callback(
            admin=admin,
            code=None,
            state="st",
            error="access_denied",
            error_description="The user denied the request",
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == (
        "http://localhost:3000/workspace/integrations/connectors?slack_error=cancelled"
    )
    # CSRF state is consumed on the cancel path too.
    assert "slack_oauth_state:st" not in redis.store


@pytest.mark.asyncio
async def test_callback_cancel_with_expired_state_still_redirects():
    """#1375: cancel with an unknown/expired state must not dead-end."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(admin=admin, code=None, state="gone", error="access_denied")

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=cancelled")


@pytest.mark.asyncio
async def test_callback_unknown_error_maps_failed_and_never_reflects():
    """#1375: non-cancel errors map to the allowlisted 'failed'; raw text never reflected."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(
            admin=admin,
            code=None,
            state=None,
            error="invalid_scope",
            error_description="<script>alert(1)</script>",
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.endswith("?slack_error=failed")
    assert "invalid_scope" not in location
    assert "script" not in location


@pytest.mark.asyncio
async def test_callback_no_code_no_error_redirects_failed():
    """#1375: neither code nor error → failed redirect, no raw 422."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(admin=admin, code=None, state=None)

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "", "state": "st"},  # empty code — "" must not reach the exchange
        {"code": "abc", "state": ""},  # empty state — "" must not reach validation
    ],
)
async def test_callback_empty_string_params_redirect_failed(kwargs):
    """#1375: FastAPI delivers ``?code=``/``?state=`` as "" — empty strings must
    take the friendly-redirect lane, not fall through to a raw 4xx JSON."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(admin=admin, **kwargs)

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
async def test_callback_empty_error_param_does_not_abort_success():
    """#1375: a proxy echoing ``?error=`` (empty) alongside a valid code/state
    must not abort the install — empty error is treated as absent."""
    from api.routes.connectors_slack import slack_callback

    ws_id = uuid4()
    redis = _FakeRedis({"slack_oauth_state:st": str(ws_id)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {
        "ok": True,
        "access_token": "xoxb-123",
        "team": {"id": "T01", "name": "Acme"},
        "authed_user": {"id": "U01"},
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=token_resp)
    http_ctx = MagicMock()
    http_ctx.__aenter__ = AsyncMock(return_value=http_client)
    http_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.httpx.AsyncClient", return_value=http_ctx),
        patch("api.routes.connectors_slack.get_encryptor", return_value=_fake_encryptor()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state="st", error="")

    assert resp.status_code == 303
    assert "slack_install=" in resp.headers["location"]
    assert "slack_error" not in resp.headers["location"]


def test_callback_query_params_not_required():
    """#1375 regression pin: a required Query param turns user-cancel into raw 422.

    The bug shape was ``code: str = Query(...)``. Both optional idioms are
    accepted — ``Annotated[str | None, Query()] = None`` and the auth.py-style
    ``str | None = Query(None)`` — so the pin fires only on a genuine
    required-param regression, not on a style refactor.
    """
    import inspect

    from fastapi.params import Query as QueryInfo

    from api.routes.connectors_slack import slack_callback

    sig = inspect.signature(slack_callback)
    for name in ("code", "state", "error", "error_description"):
        default = sig.parameters[name].default
        if isinstance(default, QueryInfo):
            default = default.default
        assert default is None, (
            f"slack_callback param {name!r} must be optional with a None "
            "default — a required Query param turns the Slack consent cancel "
            "into a raw 422 (#1375)."
        )


@pytest.mark.asyncio
async def test_callback_code_without_state_redirects_failed():
    """#1375: code present but state missing cannot be CSRF-validated → failed redirect."""
    from api.routes.connectors_slack import slack_callback

    admin = {"user_id": "u1", "current_workspace_id": uuid4()}
    with (
        patch("api.routes.connectors_slack.get_settings", return_value=_settings()),
        patch("api.routes.connectors_slack.get_redis_client", return_value=_FakeRedis()),
    ):
        resp = await slack_callback(admin=admin, code="abc", state=None)

    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?slack_error=failed")


@pytest.mark.asyncio
async def test_pending_returns_summary_without_bot_token():
    from api.routes.connectors_slack import slack_pending

    ws_id = uuid4()
    # The store uses bot_token_enc (encrypted), not bot_token.
    install = {
        "workspace_id": str(ws_id),
        "bot_token_enc": "ENC:xoxb-secret",
        "team_id": "T01",
        "team_name": "Acme",
        "installing_admin_user_id": "U01",
    }
    redis = _FakeRedis({"slack_install:h1": json.dumps(install)})
    admin = {"user_id": "u1", "current_workspace_id": ws_id}

    with patch("api.routes.connectors_slack.get_redis_client", return_value=redis):
        result = await slack_pending("h1", admin)

    assert result == {
        "team_id": "T01",
        "team_name": "Acme",
        "installing_admin_user_id": "U01",
        "app_key": "default",
    }
    assert "bot_token" not in result
    assert "bot_token_enc" not in result


@pytest.mark.asyncio
async def test_pending_404_for_other_workspace():
    from fastapi import HTTPException

    from api.routes.connectors_slack import slack_pending

    install = {"workspace_id": str(uuid4()), "team_id": "T01"}
    redis = _FakeRedis({"slack_install:h1": json.dumps(install)})
    admin = {"user_id": "u1", "current_workspace_id": uuid4()}  # different ws

    with patch("api.routes.connectors_slack.get_redis_client", return_value=redis):
        with pytest.raises(HTTPException) as exc:
            await slack_pending("h1", admin)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_peek_does_not_consume_and_discard_removes():
    """peek_slack_install is non-consuming; discard_slack_install removes the entry."""
    from api.routes import connectors_slack

    install = {"workspace_id": "ws1", "bot_token_enc": "ENC:xoxb-x", "team_id": "T01"}
    redis = _FakeRedis({"slack_install:h1": json.dumps(install)})

    with (
        patch("api.routes.connectors_slack.get_redis_client", return_value=redis),
        patch("api.routes.connectors_slack.get_encryptor", return_value=_fake_encryptor()),
    ):
        first = await connectors_slack.peek_slack_install("h1")
        second = await connectors_slack.peek_slack_install("h1")  # still present
        assert first is not None
        assert second is not None  # peek does NOT consume
        assert first["bot_token"] == "xoxb-x"  # decrypted at read time

        await connectors_slack.discard_slack_install("h1")
        third = await connectors_slack.peek_slack_install("h1")
    assert third is None  # now consumed by discard
