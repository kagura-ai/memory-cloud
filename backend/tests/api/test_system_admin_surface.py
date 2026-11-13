"""The system-admin surface follows SYSTEM_ADMIN_MODE on every gated route (#2767).

The route list comes from the live app (``routes_behind_require_admin`` walks
each route's dependency tree), and is pinned below so a new admin route shows
up here. For every one of them:

- ``off``: 404 with the body an unknown path gets, for an admin session, a
  normal session, an API key and an anonymous caller;
- ``service``: 404 outside the address allow-list, 503 without a configured
  credential, 401 for a missing / wrong / malformed credential (an admin
  session alone included), 400 for a bad ``X-Operator-Email``;
- ``session`` (default): 401 anonymous, 403 for a non-admin, as before.

No refusal reaches a handler: ``get_db`` fails the test if it is opened.
"""

from __future__ import annotations

import secrets
import typing
import uuid

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api.main import app, memory_cloud_exception_handler
from auth.dependencies import (
    get_current_user_optional,
    require_admin,
    require_admin_account,
)
from auth.history_principal import get_history_principal, set_history_principal
from auth.system_admin_mode import routes_behind_require_admin
from config.settings import get_settings
from db.base import get_db
from utils.exceptions import MemoryCloudException

# Every (method, path) behind require_admin today (55). A new admin route must
# be added here on purpose: it then follows SYSTEM_ADMIN_MODE automatically.
ADMIN_SURFACE = frozenset(
    {
        ("DELETE", "/api/v1/admin/signup-gate/allowlist/{entry_id}"),
        ("DELETE", "/api/v1/admin/system-admins/{user_id}"),
        ("DELETE", "/api/v1/admin/users/{user_id}"),
        ("GET", "/api/v1/admin/bm25-drift"),
        ("GET", "/api/v1/admin/bm25-drift/{row_id}"),
        ("GET", "/api/v1/admin/cost-aggregation"),
        ("GET", "/api/v1/admin/force-transfer-requests/{request_id}"),
        ("GET", "/api/v1/admin/memory-health"),
        ("GET", "/api/v1/admin/neural-config"),
        ("GET", "/api/v1/admin/neural-config/{key}"),
        ("GET", "/api/v1/admin/operations/status"),
        ("GET", "/api/v1/admin/plans/addon-cache-consistency"),
        ("GET", "/api/v1/admin/plans/audit"),
        ("GET", "/api/v1/admin/plans/tiers"),
        ("GET", "/api/v1/admin/plans/workspaces"),
        ("GET", "/api/v1/admin/plans/workspaces/{workspace_id}/quotas"),
        ("GET", "/api/v1/admin/referrals"),
        ("GET", "/api/v1/admin/signup-gate/allowlist"),
        ("GET", "/api/v1/admin/signup-gate/config"),
        ("GET", "/api/v1/admin/sleep-reports"),
        ("GET", "/api/v1/admin/sleep-reports/{report_id}"),
        ("GET", "/api/v1/admin/system-admins"),
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/users/{user_id}"),
        ("GET", "/api/v1/admin/users/{user_id}/stats"),
        ("GET", "/api/v1/admin/worker-apps"),
        ("GET", "/api/v1/oauth/providers"),
        ("PATCH", "/api/v1/admin/users/{user_id}/workspace_slot_bonus"),
        ("PATCH", "/api/v1/admin/worker-apps/{platform}/{app_key}"),
        ("POST", "/api/v1/admin/bm25-drift/run"),
        ("POST", "/api/v1/admin/bm25-drift/{row_id}/reveal-terms"),
        ("POST", "/api/v1/admin/contexts/recover"),
        ("POST", "/api/v1/admin/contexts/{context_id}/restore"),
        ("POST", "/api/v1/admin/embedding/retry-failed"),
        ("POST", "/api/v1/admin/force-transfer-requests/{request_id}/approve"),
        ("POST", "/api/v1/admin/force-transfer-requests/{request_id}/cancel"),
        ("POST", "/api/v1/admin/neural-config/reset"),
        ("POST", "/api/v1/admin/neural/recalibrate"),
        ("POST", "/api/v1/admin/referrals/{grant_id}/revoke"),
        ("POST", "/api/v1/admin/signup-gate/allowlist"),
        ("POST", "/api/v1/admin/sleep/actions/{action_id}/undo-merge"),
        ("POST", "/api/v1/admin/sleep/run"),
        ("POST", "/api/v1/admin/system-admins"),
        ("POST", "/api/v1/admin/users/{user_id}/erase"),
        ("POST", "/api/v1/admin/worker-apps"),
        ("POST", "/api/v1/admin/worker-apps/{platform}/{app_key}/rotate-secret"),
        ("POST", "/api/v1/admin/workspaces/{workspace_id}/force-transfer-ownership"),
        ("POST", "/api/v1/config/batch"),
        ("PUT", "/api/v1/admin/neural-config/{key}"),
        ("PUT", "/api/v1/admin/plans/workspaces/{workspace_id}/plan"),
        ("PUT", "/api/v1/admin/plans/workspaces/{workspace_id}/quotas"),
        ("PUT", "/api/v1/admin/plans/workspaces/{workspace_id}/spend-cap"),
        ("PUT", "/api/v1/admin/signup-gate/config"),
        ("PUT", "/api/v1/admin/users/{user_id}/role"),
        ("PUT", "/api/v1/config/{key}"),
    }
)

# Routes that act on the caller's own account: an operator gets 403 SYSADMIN-004.
OWN_ACCOUNT_ROUTES = frozenset(
    {
        ("GET", "/api/v1/admin/memory-health"),
        ("POST", "/api/v1/admin/sleep/run"),
        ("POST", "/api/v1/admin/sleep/actions/{action_id}/undo-merge"),
    }
)

ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
USER_SESSION = {"user_id": "1002", "email": "user@example.com", "role": "user"}
OPERATOR = "ops@example.com"


def _sample(annotation: typing.Any) -> str:
    if annotation is uuid.UUID:
        return "00000000-0000-4000-8000-000000000001"
    if annotation is int:
        return "1"
    if typing.get_origin(annotation) is typing.Literal:
        return str(typing.get_args(annotation)[0])
    return "x1"


def _concrete(context: typing.Any) -> str:
    path = context.path
    for param in context.dependant.path_params:
        path = path.replace("{" + param.name + "}", _sample(param.field_info.annotation))
    return path


_CONTEXTS = routes_behind_require_admin(app)
ROUTES = sorted(
    (method, context.path, _concrete(context))
    for context in _CONTEXTS
    for method in context.methods - {"HEAD", "OPTIONS"}
)
ROUTE_IDS = [f"{method} {path}" for method, path, _ in ROUTES]


def test_the_admin_surface_is_pinned():
    assert {(method, path) for method, path, _ in ROUTES} == ADMIN_SURFACE


def test_own_account_routes_are_behind_the_gate_too():
    assert OWN_ACCOUNT_ROUTES <= ADMIN_SURFACE


@pytest.fixture
def settings_for(monkeypatch):
    def _apply(mode: str, **values: str) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", mode)
        defaults = {
            "system_admin_service_token": "",
            "system_admin_service_token_previous": "",
            "system_admin_service_allowed_cidrs": "",
        }
        defaults.update(values)
        for field, value in defaults.items():
            monkeypatch.setattr(settings, field, value)

    return _apply


@pytest.fixture
def client():
    async def _no_db():
        raise AssertionError("a refused request must not open a database session")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = _no_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as(principal: dict | None) -> None:
    if principal is None:
        app.dependency_overrides.pop(get_current_user_optional, None)
        return

    async def _session():
        return dict(principal)

    app.dependency_overrides[get_current_user_optional] = _session


def _unknown_path_404(client: TestClient) -> dict:
    response = client.get("/api/v1/admin/__no_such_route__")
    assert response.status_code == 404
    return response.json()


# --- off ----------------------------------------------------------------------

CALLERS = {
    "admin_session": (ADMIN_SESSION, {}),
    "normal_session": (USER_SESSION, {}),
    "api_key": (
        None,
        {"Authorization": "Bearer kagura_" + "a" * 40, "X-API-Key": "kagura_" + "a" * 40},
    ),
    "anonymous": (None, {}),
}


@pytest.mark.parametrize("caller", sorted(CALLERS))
@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_off_answers_like_an_unknown_path(client, settings_for, caller, method, path, url):
    settings_for("off")
    principal, headers = CALLERS[caller]
    _as(principal)
    expected = _unknown_path_404(client)
    response = client.request(method, url, headers=headers)
    assert response.status_code == 404, response.text
    assert (
        response.json() == expected == {"error": "HTTP-404", "message": "Not Found", "details": {}}
    )


def test_off_ignores_a_valid_service_credential(client, settings_for):
    token = secrets.token_hex(32)
    settings_for("off", system_admin_service_token=token)
    response = client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR},
    )
    assert response.status_code == 404


def test_off_with_a_broken_json_body_never_reaches_the_handler(client, settings_for):
    # Residual documented in docs/deployment.md: FastAPI decodes a JSON body
    # before it solves dependencies, so a broken body is 422, not 404. It
    # carries no admin data and the handler never runs (get_db would fail).
    settings_for("off")
    _as(ADMIN_SESSION)
    response = client.post(
        "/api/v1/admin/system-admins",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "VAL-001"


# --- service ------------------------------------------------------------------


@pytest.fixture
def service(settings_for):
    token = secrets.token_hex(32)
    settings_for("service", system_admin_service_token=token)
    return token


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_service_refuses_an_admin_session_without_the_credential(
    client, service, method, path, url
):
    _as(ADMIN_SESSION)
    response = client.request(method, url)
    assert response.status_code == 401, response.text
    assert response.json()["error"] == "SYSADMIN-002"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_service_without_a_configured_credential_is_503(client, settings_for, method, path, url):
    settings_for("service")
    response = client.request(
        method, url, headers={"Authorization": "Bearer " + "a" * 64, "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "SYSADMIN-001"


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_service_with_a_wrong_credential_is_401(client, service, method, path, url):
    response = client.request(
        method, url, headers={"Authorization": "Bearer " + "b" * 64, "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"] == "SYSADMIN-002"


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_service_with_a_bad_operator_email_is_400(client, service, method, path, url):
    response = client.request(
        method,
        url,
        headers={"Authorization": f"Bearer {service}", "X-Operator-Email": "not-an-address"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "SYSADMIN-003"
    assert "not-an-address" not in response.text


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_service_outside_the_address_allow_list_is_404(client, settings_for, method, path, url):
    token = secrets.token_hex(32)
    settings_for(
        "service", system_admin_service_token=token, system_admin_service_allowed_cidrs="10.0.0.0/8"
    )
    expected = _unknown_path_404(client)
    response = client.request(
        method, url, headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 404, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(b"Bearer \xe9\xe9\xe9", id="non-ascii"),
        pytest.param(b"Bearer ", id="empty-bearer"),
        pytest.param(b"Bearer", id="bare-scheme"),
        pytest.param(b"Basic dXNlcjpwYXNz", id="basic"),
        pytest.param(None, id="missing"),
    ],
)
def test_service_malformed_credentials_are_401_never_500(client, service, authorization):
    headers: dict = {"X-Operator-Email": OPERATOR}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = client.get("/api/v1/admin/users", headers=headers)
    assert response.status_code == 401, response.text
    assert response.json()["error"] == "SYSADMIN-002"


def test_service_lower_case_bearer_scheme_is_401(client, service):
    response = client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"bearer {service}", "X-Operator-Email": OPERATOR},
    )
    assert response.status_code == 401


def test_service_repeated_operator_header_is_400(client, service):
    response = client.get(
        "/api/v1/admin/users",
        headers=[
            ("Authorization", f"Bearer {service}"),
            ("X-Operator-Email", OPERATOR),
            ("X-Operator-Email", "other@example.com"),
        ],
    )
    assert response.status_code == 400
    assert response.json()["error"] == "SYSADMIN-003"


def test_service_checks_the_credential_before_the_operator_header(client, service):
    # An unauthenticated caller learns nothing about the header rules.
    response = client.get("/api/v1/admin/users", headers={"X-Operator-Email": "bad"})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), sorted(OWN_ACCOUNT_ROUTES))
def test_service_operator_on_an_own_account_route_is_403(client, service, method, path):
    url = next(concrete for m, p, concrete in ROUTES if (m, p) == (method, path))
    response = client.request(
        method, url, headers={"Authorization": f"Bearer {service}", "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "SYSADMIN-004"


def test_service_recover_apply_is_refused_for_an_operator_before_any_work(
    client, service, monkeypatch
):
    def _no_qdrant():
        raise AssertionError("recover must refuse before touching the vector store")

    monkeypatch.setattr("db.qdrant.get_qdrant_client", _no_qdrant)
    app.dependency_overrides[get_db] = _fake_db
    response = client.post(
        "/api/v1/admin/contexts/recover",
        json={"context_id": "not-even-a-uuid", "dry_run": False},
        headers={"Authorization": f"Bearer {service}", "X-Operator-Email": OPERATOR},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "SYSADMIN-004"


async def _fake_db():
    from unittest.mock import MagicMock

    yield MagicMock()


# --- session (default) --------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "url"), ROUTES, ids=ROUTE_IDS)
def test_session_default_is_unchanged(client, settings_for, method, path, url):
    settings_for("session")
    _as(None)
    anonymous = client.request(method, url)
    assert anonymous.status_code == 401, anonymous.text
    _as(USER_SESSION)
    user = client.request(method, url)
    assert user.status_code == 403, user.text
    assert user.json() == {"error": "HTTP-403", "message": "Admin role required", "details": {}}


def test_session_ignores_service_headers(client, settings_for):
    token = secrets.token_hex(32)
    settings_for("session", system_admin_service_token=token)
    _as(None)
    response = client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR},
    )
    assert response.status_code == 401


# --- the principal handed to handlers ----------------------------------------


def _probe_app() -> FastAPI:
    probe = FastAPI()
    probe.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]

    @probe.get("/probe")
    async def _probe(request: Request, admin: dict = Depends(require_admin)) -> dict:
        return {
            "admin": admin,
            "history_kind": get_history_principal().kind,
            "state_user": getattr(request.state, "user", "unset"),
            "state_user_id": getattr(request.state, "user_id", "unset"),
        }

    @probe.get("/own")
    async def _own(admin: dict = Depends(require_admin_account)) -> dict:
        return {"user_id": admin["user_id"]}

    @probe.middleware("http")
    async def _cookie_user(request: Request, call_next):
        # Stands in for SessionMiddleware: a relayed admin cookie.
        request.state.user = dict(ADMIN_SESSION)
        request.state.user_id = ADMIN_SESSION["user_id"]
        return await call_next(request)

    return probe


def test_service_operator_principal_replaces_the_cookie_user(service):
    set_history_principal("session")
    response = TestClient(_probe_app()).get(
        "/probe",
        headers={"Authorization": f"Bearer {service}", "X-Operator-Email": " Ops@Example.com "},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admin"] == {
        "user_id": "operator:ops@example.com",
        "sub": "operator:ops@example.com",
        "email": "operator:ops@example.com",
        "name": "operator:ops@example.com",
        "role": "operator",
        "principal_kind": "operator",
        "operator_email": "ops@example.com",
        "current_workspace_id": None,
    }
    assert body["history_kind"] == "operator"
    # R10: usage and attribution never fall to the relayed cookie's user.
    assert body["state_user"] is None
    assert body["state_user_id"] is None


def test_service_accepts_a_previous_credential_and_logs_the_slot(settings_for):
    from structlog.testing import capture_logs

    current, previous = secrets.token_hex(32), secrets.token_hex(32)
    settings_for(
        "service",
        system_admin_service_token=current,
        system_admin_service_token_previous=previous,
    )
    with capture_logs() as logs:
        response = TestClient(_probe_app()).get(
            "/probe", headers={"Authorization": f"Bearer {previous}", "X-Operator-Email": OPERATOR}
        )
    assert response.status_code == 200
    used = [entry for entry in logs if entry["event"] == "system_admin_service_token_previous_used"]
    assert used == [
        {"event": "system_admin_service_token_previous_used", "slot": 1, "log_level": "warning"}
    ]
    assert previous not in repr(logs) and current not in repr(logs)
    with capture_logs() as logs:
        TestClient(_probe_app()).get(
            "/probe", headers={"Authorization": f"Bearer {current}", "X-Operator-Email": OPERATOR}
        )
    assert not [
        entry for entry in logs if entry["event"] == "system_admin_service_token_previous_used"
    ]


def test_service_logs_failed_attempts_without_the_header(service):
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        TestClient(_probe_app()).get("/probe", headers={"Authorization": "Bearer guess-1234"})
    failed = [entry for entry in logs if entry["event"] == "system_admin_service_auth_failed"]
    assert len(failed) == 1
    assert failed[0]["path"] == "/probe"
    assert "guess-1234" not in repr(logs)


def test_service_inside_the_address_allow_list_passes(settings_for):
    token = secrets.token_hex(32)
    settings_for(
        "service", system_admin_service_token=token, system_admin_service_allowed_cidrs="10.0.0.0/8"
    )
    inside = TestClient(_probe_app(), client=("10.1.2.3", 50000))
    response = inside.get(
        "/probe", headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 200
    mapped = TestClient(_probe_app(), client=("::ffff:10.1.2.3", 50000))
    assert (
        mapped.get(
            "/probe", headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
        ).status_code
        == 200
    )


def test_service_own_account_dependency_refuses_operators_only(settings_for, service):
    probe = TestClient(_probe_app())
    refused = probe.get(
        "/own", headers={"Authorization": f"Bearer {service}", "X-Operator-Email": OPERATOR}
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == "SYSADMIN-004"
    settings_for("session")
    allowed = probe.get("/own")  # the stand-in middleware supplies an admin cookie
    assert allowed.status_code == 200
    assert allowed.json() == {"user_id": ADMIN_SESSION["user_id"]}


# --- startup log line ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "level"), [("session", "info"), ("off", "info"), ("service", "warning")]
)
def test_startup_logs_the_mode_never_the_credential(settings_for, value, level):
    from structlog.testing import capture_logs

    from api.main import _log_system_admin_mode

    token = secrets.token_hex(32)
    settings_for(value, system_admin_service_token=token)
    with capture_logs() as logs:
        _log_system_admin_mode()
    [entry] = [e for e in logs if e["event"] == "system_admin_mode"]
    assert entry["mode"] == value
    assert entry["log_level"] == level
    assert entry["service_token_configured"] is True
    assert token not in repr(logs)
