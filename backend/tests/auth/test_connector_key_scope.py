"""Where a connector key is accepted (#2929).

A connector key (an ``api_keys`` row linked to a connector, or one carrying
the reserved ``connector:`` name) authenticates the MCP tools a connector
worker uses and no REST route. ``auth.connector_key_scope`` holds both lists
and the one decision; these tests pin the module, the setting, the
``VerifiedKey`` flag and the two REST authentication dependencies. The sweep
over the real route table is ``tests/integration/test_connector_key_scope_routes_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from auth.api_keys import APIKeyManager, VerifiedKey
from auth.connector_key_scope import (
    CONNECTOR_KEY_MCP_TOOLS,
    CONNECTOR_KEY_REST_ROUTES,
    check_connector_key_scope,
    connector_key_mcp_tool_allowed,
    connector_key_rest_allowed,
    connector_key_scope_mode,
)
from auth.connector_keys import connector_key_name
from auth.dependencies import APIKeyOrSessionUser, APIKeyUser
from config.settings import Settings, get_settings
from utils.exceptions import ConnectorKeyScopeError
from utils.hashing import sha256_hex

WORKSPACE_ID = uuid4()
CONNECTOR_ID = uuid4()
PLAINTEXT = "kagura_" + "q" * 24


# ---------------------------------------------------------------------------
# The lists
# ---------------------------------------------------------------------------


def test_no_rest_route_accepts_a_connector_key() -> None:
    """Adding a REST route here is a visible change in two places."""
    assert CONNECTOR_KEY_REST_ROUTES == frozenset()


def test_the_mcp_list_is_the_connector_worker_tools() -> None:
    assert CONNECTOR_KEY_MCP_TOOLS == frozenset(
        {
            "recall",
            "remember",
            "create_edge",
            "reference",
            "update_memory",
            "list_edges",
            "delete_edge",
            "forget",
            "get_context_info",
        }
    )


def test_rest_decision_reads_the_method_and_the_route_template() -> None:
    listed = frozenset({("POST", "/api/v1/memory/remember")})
    with patch("auth.connector_key_scope.CONNECTOR_KEY_REST_ROUTES", listed):
        assert connector_key_rest_allowed("POST", "/api/v1/memory/remember") is True
        assert connector_key_rest_allowed("GET", "/api/v1/memory/remember") is False
        assert connector_key_rest_allowed("HEAD", "/api/v1/memory/remember") is False
        assert connector_key_rest_allowed("POST", "/memory/remember") is False
        # No matched route: never accepted.
        assert connector_key_rest_allowed("POST", None) is False


def test_mcp_decision_reads_the_tool_name() -> None:
    for name in CONNECTOR_KEY_MCP_TOOLS:
        assert connector_key_mcp_tool_allowed(name) is True
    for name in ("setup_connector", "create_context", "describe_tools", "guide", "", None, 5):
        assert connector_key_mcp_tool_allowed(name) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The decision function and its two modes
# ---------------------------------------------------------------------------


@pytest.fixture
def scope_mode(monkeypatch):
    """Set ``CONNECTOR_KEY_SCOPE`` on the cached settings for one test."""

    def set_mode(mode: str) -> None:
        monkeypatch.setattr(get_settings(), "connector_key_scope", mode)

    return set_mode


def _check(allowed: bool) -> None:
    check_connector_key_scope(
        surface="rest",
        target="POST /api/v1/resource-tokens",
        allowed=allowed,
        key_prefix="kagura_abcd",
        connector_id=CONNECTOR_ID,
    )


def test_an_allowed_target_passes_silently(scope_mode) -> None:
    scope_mode("enforce")
    with patch("auth.connector_key_scope.logger") as logger:
        _check(True)
    logger.warning.assert_not_called()


def test_enforce_refuses_with_the_typed_error_and_one_event(scope_mode) -> None:
    scope_mode("enforce")
    with patch("auth.connector_key_scope.logger") as logger:
        with pytest.raises(ConnectorKeyScopeError) as exc_info:
            _check(False)
    exc = exc_info.value
    assert (exc.status_code, exc.error_code) == (403, "AUTH-204")
    assert exc.message == "This credential is not accepted on this route"
    assert exc.details == {}
    assert exc.log_level == "warning"
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args == ("connector_key_scope_refused",)
    assert logger.warning.call_args.kwargs == {
        "surface": "rest",
        "target": "POST /api/v1/resource-tokens",
        "key_prefix": "kagura_abcd",
        "connector_id": str(CONNECTOR_ID),
    }


def test_log_mode_logs_one_event_and_lets_the_request_through(scope_mode) -> None:
    scope_mode("log")
    with patch("auth.connector_key_scope.logger") as logger:
        _check(False)
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args == ("connector_key_scope_would_refuse",)
    assert logger.warning.call_args.kwargs["target"] == "POST /api/v1/resource-tokens"


def test_the_mcp_refusal_names_a_tool(scope_mode) -> None:
    scope_mode("enforce")
    with pytest.raises(ConnectorKeyScopeError) as exc_info:
        check_connector_key_scope(
            surface="mcp",
            target="setup_connector",
            allowed=False,
            key_prefix=None,
            connector_id=None,
        )
    assert exc_info.value.message == "This credential is not accepted for this tool"


# ---------------------------------------------------------------------------
# CONNECTOR_KEY_SCOPE
# ---------------------------------------------------------------------------


def test_the_setting_defaults_to_enforce(monkeypatch) -> None:
    monkeypatch.delenv("CONNECTOR_KEY_SCOPE", raising=False)
    assert Settings(_env_file=None).connector_key_scope == "enforce"


@pytest.mark.parametrize(
    ("raw", "expected"), [("enforce", "enforce"), ("log", "log"), (" log\n", "log")]
)
def test_the_setting_accepts_enforce_and_log(monkeypatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("CONNECTOR_KEY_SCOPE", raw)
    assert Settings(_env_file=None).connector_key_scope == expected


@pytest.mark.parametrize("raw", ["", " ", "off", "Log", "ENFORCE", "shadow", "true"])
def test_any_other_value_refuses_to_start(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("CONNECTOR_KEY_SCOPE", raw)
    with pytest.raises(ValidationError, match="connector_key_scope"):
        Settings(_env_file=None)


def test_the_mode_accessor_reads_the_setting(scope_mode) -> None:
    scope_mode("log")
    assert connector_key_scope_mode() == "log"
    scope_mode("enforce")
    assert connector_key_scope_mode() == "enforce"


# ---------------------------------------------------------------------------
# VerifiedKey.connector_scoped (set by verify_key)
# ---------------------------------------------------------------------------


def _key_record(name: str, *, connector_id: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        public_id="akey_" + "c" * 22,
        user_id="owner-1",
        name=name,
        workspace_id=WORKSPACE_ID,
        bound_context_id=None,
        agent_id=None,
        default_context_id=None,
        connector_id=connector_id,
        key_prefix="kagura_s",
        key_hash=sha256_hex(PLAINTEXT),
        revoked_at=None,
        expires_at=None,
        last_used_at=None,
    )


def _result(value: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


async def _verify(*rows: object | None) -> VerifiedKey | None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(r) for r in rows])
    db.flush = AsyncMock()
    with (
        patch("auth.api_keys.key_owner_holds_workspace", new=AsyncMock(return_value=True)),
        patch("auth.api_keys.utcnow", return_value=datetime(2026, 11, 19, 12, 0, 0)),
        patch("services.workspace_slot_gate.slot_suspended", new=AsyncMock(return_value=False)),
    ):
        return await APIKeyManager(db).verify_key(PLAINTEXT)


@pytest.mark.asyncio
async def test_a_key_linked_to_a_connector_is_connector_scoped() -> None:
    workspace = SimpleNamespace(id=WORKSPACE_ID, plan_name="pro", effective_max_connectors=3)
    verified = await _verify(
        _key_record(connector_key_name(CONNECTOR_ID), connector_id=CONNECTOR_ID), workspace
    )
    assert verified is not None
    assert verified.is_connector_key is True
    assert verified.connector_scoped is True
    assert verified.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_a_key_with_the_reserved_name_and_no_link_is_connector_scoped() -> None:
    """A key minted for a connector before the link existed: the reserved name
    alone puts it under the connector key scope; the link-based flag stays off."""
    verified = await _verify(_key_record(connector_key_name(CONNECTOR_ID)))
    assert verified is not None
    assert verified.is_connector_key is False
    assert verified.connector_scoped is True
    assert verified.connector_id is None


@pytest.mark.asyncio
async def test_an_ordinary_key_is_not_connector_scoped() -> None:
    verified = await _verify(_key_record("slack connector laptop"))
    assert verified is not None
    assert (verified.is_connector_key, verified.connector_scoped) == (False, False)


def test_a_verified_key_built_elsewhere_is_not_connector_scoped() -> None:
    assert VerifiedKey(1, "u", None, None).connector_scoped is False


# ---------------------------------------------------------------------------
# The REST dependencies, over a router included with a prefix
# ---------------------------------------------------------------------------

_CONNECTOR = VerifiedKey(
    id=1,
    user_id="owner-1",
    workspace_id=WORKSPACE_ID,
    bound_context_id=None,
    key_prefix="kagura_conn",
    is_connector_key=True,
    public_id="akey_" + "d" * 22,
    connector_id=CONNECTOR_ID,
    connector_scoped=True,
)
_LEGACY_CONNECTOR = _CONNECTOR._replace(is_connector_key=False, connector_id=None)
_ORDINARY = VerifiedKey(
    id=2,
    user_id="owner-1",
    workspace_id=WORKSPACE_ID,
    bound_context_id=None,
    key_prefix="kagura_mine",
    public_id="akey_" + "e" * 22,
)
_AGENT = _ORDINARY._replace(id=3, agent_id=uuid4(), agent_enforcement_mode="enforce")


@pytest.fixture
def probe():
    """An app with the two dependencies on a prefixed router, like the real one.

    The ``x-test-session`` header plays the session middleware: a request may
    carry a signed-in browser session next to the bearer.
    """
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from api.main import http_exception_handler, memory_cloud_exception_handler
    from db.base import get_db
    from utils.exceptions import MemoryCloudException

    handled: list[str] = []
    router = APIRouter(prefix="/probe")

    @router.get("/items/{item_id}")
    async def either(item_id: str, user: APIKeyOrSessionUser) -> dict:
        handled.append("either")
        return {"user_id": user["user_id"]}

    @router.post("/keys-only")
    async def keys_only(user: APIKeyUser) -> dict:
        handled.append("keys_only")
        return {"user_id": user["user_id"]}

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    @app.middleware("http")
    async def browser_session(request: Request, call_next):
        user_id = request.headers.get("x-test-session")
        request.state.user = {"user_id": user_id} if user_id else None
        return await call_next(request)

    async def no_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = no_db
    client = TestClient(app, raise_server_exceptions=False)

    def call(method: str, path: str, verified: VerifiedKey | None, **headers: str):
        with patch.object(APIKeyManager, "verify_key", new=AsyncMock(return_value=verified)):
            return client.request(
                method, path, headers={"Authorization": f"Bearer {PLAINTEXT}", **headers}
            )

    return SimpleNamespace(call=call, handled=handled)


_REFUSAL = {
    "error": "AUTH-204",
    "message": "This credential is not accepted on this route",
    "details": {},
}


@pytest.mark.parametrize(
    "verified", [_CONNECTOR, _LEGACY_CONNECTOR], ids=["linked", "reserved-name"]
)
def test_a_connector_key_is_refused_by_the_api_key_or_session_dependency(
    probe, scope_mode, verified
) -> None:
    scope_mode("enforce")
    with patch("auth.connector_key_scope.logger") as logger:
        response = probe.call("GET", "/api/v1/probe/items/7", verified)
    assert response.status_code == 403
    assert response.json() == _REFUSAL
    assert probe.handled == []
    # The decision read the template FastAPI matched, prefixes included.
    assert logger.warning.call_args.kwargs["target"] == "GET /api/v1/probe/items/{item_id}"
    assert logger.warning.call_args.kwargs["key_prefix"] == "kagura_conn"


def test_a_refused_connector_key_never_falls_back_to_the_session(probe, scope_mode) -> None:
    scope_mode("enforce")
    response = probe.call("GET", "/api/v1/probe/items/7", _CONNECTOR, **{"x-test-session": "s-1"})
    assert response.status_code == 403
    assert response.json() == _REFUSAL
    assert probe.handled == []


def test_a_connector_key_is_refused_by_the_api_key_dependency(probe, scope_mode) -> None:
    scope_mode("enforce")
    with patch("auth.connector_key_scope.logger") as logger:
        response = probe.call("POST", "/api/v1/probe/keys-only", _CONNECTOR)
    assert response.status_code == 403
    assert response.json() == _REFUSAL
    assert probe.handled == []
    assert logger.warning.call_args.kwargs["target"] == "POST /api/v1/probe/keys-only"


@pytest.mark.parametrize(
    ("method", "path", "handler"),
    [("GET", "/api/v1/probe/items/7", "either"), ("POST", "/api/v1/probe/keys-only", "keys_only")],
)
def test_log_mode_answers_as_the_route_does_and_logs_once(
    probe, scope_mode, method, path, handler
) -> None:
    scope_mode("log")
    with patch("auth.connector_key_scope.logger") as logger:
        response = probe.call(method, path, _CONNECTOR)
    assert response.status_code == 200
    assert response.json() == {"user_id": "owner-1"}
    assert probe.handled == [handler]
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args == ("connector_key_scope_would_refuse",)
    assert PLAINTEXT not in repr(logger.warning.call_args)


@pytest.mark.parametrize("verified", [_ORDINARY, _AGENT], ids=["ordinary", "agent"])
@pytest.mark.parametrize("mode", ["enforce", "log"])
def test_other_keys_are_not_affected(probe, scope_mode, verified, mode) -> None:
    scope_mode(mode)
    with patch("auth.connector_key_scope.logger") as logger:
        either = probe.call("GET", "/api/v1/probe/items/7", verified)
        keys_only = probe.call("POST", "/api/v1/probe/keys-only", verified)
    assert (either.status_code, keys_only.status_code) == (200, 200)
    logger.warning.assert_not_called()


def test_an_unknown_key_keeps_its_401(probe, scope_mode) -> None:
    scope_mode("enforce")
    response = probe.call("GET", "/api/v1/probe/items/7", None)
    assert response.status_code == 401


def test_a_listed_route_lets_the_connector_key_through(probe, scope_mode) -> None:
    scope_mode("enforce")
    listed = frozenset({("GET", "/api/v1/probe/items/{item_id}")})
    with (
        patch("auth.connector_key_scope.CONNECTOR_KEY_REST_ROUTES", listed),
        patch("auth.connector_key_scope.logger") as logger,
    ):
        allowed = probe.call("GET", "/api/v1/probe/items/7", _CONNECTOR)
        other = probe.call("POST", "/api/v1/probe/keys-only", _CONNECTOR)
    assert allowed.status_code == 200
    assert other.status_code == 403
    assert logger.warning.call_count == 1
