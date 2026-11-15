"""Values the database cannot take answer 422 on the admin and workspace routes (#2857).

No stored text holds a NUL, an id column is a 32- or 64-bit integer, a path
id is a UUID, and a date or time outside years 1 to 9999 is no time at all.
Each such value is refused by the parameter's declaration: the request ends
as a 422 ``VAL-001`` before any statement holds the value, and no log line
holds it beside the request path.

The largest accepted values are answered by PostgreSQL in
``tests/integration/test_param_bounds_sweep_db.py``.
"""

from __future__ import annotations

import json
import secrets
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from auth.dependencies import get_current_user_optional
from auth.sync_identity import sync_manager, sync_reader
from config.settings import get_settings
from db.base import get_db

ADMIN = "/api/v1/admin"
WORKSPACE = f"/api/v1/workspaces/{uuid4()}"
CONTEXT = uuid4()
OWNER = "owner-2857"
PAST_BIGINT = 2**63
PAST_INT32 = 2**31
# The value under test is ``{value}``; a NUL is added where the value is text.
NUL = "%00"

# (method, url, body): a NUL in a text value, an integer past its column,
# a path id that is not a UUID, a time or delay outside the calendar.
REFUSED = [
    # admin.py
    ("GET", f"{ADMIN}/users/{{value}}{NUL}", None),
    ("GET", f"{ADMIN}/users/{{value}}{NUL}/stats", None),
    ("PATCH", f"{ADMIN}/users/{{value}}{NUL}/workspace_slot_bonus", {"delta": 1}),
    ("POST", f"{ADMIN}/embedding/retry-failed?context_id={{value}}", None),
    ("POST", f"{ADMIN}/embedding/retry-failed?workspace_id={{value}}{NUL}", None),
    (
        "POST",
        f"{ADMIN}/workspaces/{uuid4()}/force-transfer-ownership",
        {"target_user_id": "{value}\u0000", "reason": "the owner left"},
    ),
    (
        "POST",
        f"{ADMIN}/contexts/recover",
        {"context_id": str(CONTEXT), "context_name": "{value}\u0000"},
    ),
    # admin_plans.py
    ("PUT", f"{ADMIN}/plans/workspaces/{{value}}/plan", {"plan_name": "pro"}),
    ("PUT", f"{ADMIN}/plans/workspaces/{{value}}{NUL}/plan", {"plan_name": "pro"}),
    ("PUT", f"{ADMIN}/plans/workspaces/{{value}}/quotas", {"addon_memory_bonus": 1}),
    ("PUT", f"{ADMIN}/plans/workspaces/{{value}}/spend-cap", {"embedding_daily_cap_usd": 1}),
    (
        "PUT",
        f"{ADMIN}/plans/workspaces/{uuid4()}/plan",
        {"plan_name": "pro", "reason": "{value}\u0000"},
    ),
    # neural_config.py
    ("GET", f"{ADMIN}/neural-config?category={{value}}{NUL}", None),
    ("GET", f"{ADMIN}/neural-config/{{value}}{NUL}", None),
    ("PUT", f"{ADMIN}/neural-config/{{value}}{NUL}", {"value": "1"}),
    ("PUT", f"{ADMIN}/neural-config/top_m_edges", {"value": "{value}\u0000"}),
    # bm25_drift.py
    ("GET", f"{ADMIN}/bm25-drift?offset={PAST_BIGINT}&_={{value}}", None),
    ("GET", f"{ADMIN}/bm25-drift/{PAST_BIGINT}?_={{value}}", None),
    (
        "POST",
        f"{ADMIN}/bm25-drift/{PAST_BIGINT}/reveal-terms",
        {"reason": "a reason long enough {value}"},
    ),
    # admin_sleep.py
    ("POST", f"{ADMIN}/sleep/actions/{PAST_BIGINT}/undo-merge?_={{value}}", None),
    # system_admins.py
    ("POST", f"{ADMIN}/system-admins", {"user_id": "{value}\u0000"}),
    # admin_referrals.py
    ("POST", f"{ADMIN}/referrals/{uuid4()}/revoke", {"reason": "{value}\u0000"}),
    # worker_apps.py
    (
        "POST",
        f"{ADMIN}/worker-apps",
        {
            "platform": "slack",
            "app_key": "app-2857",
            "display_name": "{value}\u0000",
            "signing_secret": "s",
        },
    ),
    ("PATCH", f"{ADMIN}/worker-apps/slack/app-2857", {"display_name": "{value}\u0000"}),
    # workspaces.py
    ("PUT", WORKSPACE, {"name": "{value}\u0000"}),
    ("PUT", WORKSPACE, {"description": "{value}\u0000"}),
    ("POST", f"{WORKSPACE}/members", {"user_id": "{value}\u0000", "role": "member"}),
    ("PUT", f"{WORKSPACE}/members/{{value}}{NUL}", {"role": "member"}),
    ("DELETE", f"{WORKSPACE}/members/{{value}}{NUL}", None),
    ("GET", f"{WORKSPACE}/members/{{value}}{NUL}/context-access", None),
    ("PUT", f"{WORKSPACE}/members/{{value}}{NUL}/context-access", {"allowed_context_ids": []}),
    ("POST", f"{WORKSPACE}/transfer-ownership", {"target_user_id": "{value}\u0000"}),
    ("GET", f"{WORKSPACE}/memory-timeline?days=-{PAST_BIGINT}&_={{value}}", None),
    ("GET", f"{WORKSPACE}/contexts/{CONTEXT}/usage-timeline?days=-{PAST_BIGINT}&_={{value}}", None),
    ("GET", f"{WORKSPACE}/contexts/{CONTEXT}/user-activity?days=-{PAST_BIGINT}&_={{value}}", None),
    (
        "GET",
        f"{WORKSPACE}/contexts/{CONTEXT}/public-api-stats?days=-{PAST_BIGINT}&_={{value}}",
        None,
    ),
    # member_credentials.py
    ("GET", f"{WORKSPACE}/members/{{value}}{NUL}/credentials", None),
    ("DELETE", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/api-key", None),
    ("POST", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/api-key/hide", None),
    ("POST", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/api-key/regenerate", None),
    ("POST", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/api-keys", {"name": "k"}),
    (
        "DELETE",
        f"{WORKSPACE}/members/{{value}}{NUL}/credentials/api-keys/akey_0000000000000000000000",
        None,
    ),
    ("DELETE", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/oauth", None),
    ("POST", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/oauth/hide", None),
    ("POST", f"{WORKSPACE}/members/{{value}}{NUL}/credentials/oauth/regenerate", None),
    ("POST", f"{WORKSPACE}/members/{OWNER}/credentials/api-keys", {"name": "{value}\u0000"}),
    (
        "POST",
        f"{WORKSPACE}/members/{OWNER}/credentials/api-keys?_={{value}}",
        {"name": "k", "auto_hide_minutes": PAST_BIGINT},
    ),
    (
        "POST",
        f"{WORKSPACE}/members/{OWNER}/credentials/api-keys?_={{value}}",
        {"name": "k", "auto_hide_minutes": -(10**10)},
    ),
    # operation_history.py
    ("GET", f"{WORKSPACE}/operation-history?query={{value}}{NUL}", None),
    ("GET", f"{WORKSPACE}/operation-history?actor={{value}}{NUL}", None),
    ("GET", f"{WORKSPACE}/operation-history?since=0001-01-01T00:00:00%2B05:00&_={{value}}", None),
    ("GET", f"{WORKSPACE}/operation-history?until=9999-12-31T23:59:59-05:00&_={{value}}", None),
    # workspace_plan.py
    ("GET", "/api/v1/workspaces/{value}/plan", None),
    ("GET", f"/api/v1/workspaces/{{value}}{NUL}/plan", None),
    # sleep_reports.py
    ("GET", f"{WORKSPACE}/sleep-reports?user_id={{value}}{NUL}", None),
    ("GET", f"{WORKSPACE}/sleep-reports?offset={PAST_BIGINT}&_={{value}}", None),
]


class _Empty:
    rowcount = 0

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def first(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


def _sql(statement) -> str:
    try:
        compiled = statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    except Exception:  # noqa: BLE001 - a statement that cannot render its values
        compiled = statement.compile(dialect=postgresql.dialect())
        return f"{compiled} {compiled.params!r}"
    return str(compiled)


@pytest.fixture
def session() -> MagicMock:
    """A session that answers every statement with no row and keeps the SQL."""
    db = MagicMock()
    db.statements = []

    async def _execute(statement, *args, **kwargs):
        db.statements.append(_sql(statement))
        return _Empty()

    db.execute = AsyncMock(side_effect=_execute)
    db.scalar = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    admin = {"user_id": "admin-2857", "email": "admin@example.com", "role": "admin"}
    owner = {"sub": OWNER, "user_id": OWNER, "email": "owner@example.com", "role": "user"}

    async def _db():
        yield session

    async def _admin():
        return dict(admin)

    async def _signed_in(scope, receive, send):
        # The session middleware's principal for the workspace routes.
        if scope["type"] == "http" and not scope["path"].startswith(ADMIN):
            scope.setdefault("state", {})["user"] = dict(owner)
        await app(scope, receive, send)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    yield TestClient(_signed_in, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _request(client: TestClient, method: str, url: str, body, value: str):
    payload = None if body is None else json.loads(json.dumps(body).replace("{value}", value))
    return client.request(method, url.replace("{value}", value), json=payload)


def _beside_the_path(events: list[dict]) -> str:
    # A path value is part of the URL, so the request path names it, as an
    # access log does; no other field of a log line holds it.
    return json.dumps(
        [{key: field for key, field in event.items() if key != "path"} for event in events],
        default=str,
    )


@pytest.mark.parametrize(
    ("method", "url", "body"), REFUSED, ids=[f"{m} {u[-60:]}" for m, u, _ in REFUSED]
)
def test_a_value_the_database_cannot_take_is_a_422_before_any_statement_holds_it(
    client, session, method, url, body
):
    value = f"refused{secrets.token_hex(6)}"
    with structlog.testing.capture_logs() as events:
        response = _request(client, method, url, body, value)

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert value not in response.text
    assert not any(value in statement for statement in session.statements)
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    assert value not in _beside_the_path(events)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{ADMIN}/users/{{value}}{NUL}"),
        ("PUT", f"{ADMIN}/plans/workspaces/{{value}}/plan"),
        ("GET", f"{WORKSPACE}/members/{{value}}{NUL}/credentials"),
        ("GET", f"{WORKSPACE}/sleep-reports?user_id={{value}}{NUL}"),
        ("GET", f"{WORKSPACE}/operation-history?actor={{value}}{NUL}"),
    ],
)
def test_a_refused_value_writes_no_log_line_that_holds_it(client, session, method, url):
    value = f"refused{secrets.token_hex(6)}"
    body = {"plan_name": "pro"} if method == "PUT" else None
    with structlog.testing.capture_logs() as events:
        response = _request(client, method, url, body, value)
    assert response.status_code == 422, response.text
    if "?" in url:
        # A query value is not part of the path: no field holds it.
        assert value not in json.dumps(events, default=str)
    else:
        assert value not in _beside_the_path(events)


def test_retry_failed_answers_a_bad_id_as_the_new_reads_do(client, session):
    for query in ("context_id=not-a-uuid", "workspace_id=a%00b"):
        response = client.post(f"{ADMIN}/embedding/retry-failed?{query}")
        assert response.status_code == 422, query
        assert response.json()["error"] == "VAL-001"
    # An empty value is still no filter, and no filter at all is still a 400.
    assert client.post(f"{ADMIN}/embedding/retry-failed?context_id=").status_code == 400
    assert session.statements == []


@pytest.mark.parametrize(
    "url",
    [
        f"{WORKSPACE}/memory-timeline?days=-1",
        f"{WORKSPACE}/memory-timeline?days={PAST_BIGINT}",
        f"{WORKSPACE}/contexts/{CONTEXT}/usage-timeline?days=-100000",
        f"{WORKSPACE}/operation-history?since=0001-01-01T00:00:00-05:00",
        f"{WORKSPACE}/operation-history?until=9999-12-31T23:59:59%2B05:00",
    ],
)
def test_an_accepted_window_or_time_reaches_the_route(client, session, url):
    response = client.get(url)
    assert (
        response.status_code != 422
        or response.json()["error"] != "VAL-001"
        or ("Request validation failed" not in response.text)
    ), response.text
    assert session.statements != []


# The sync surface answers every refused request with its fixed, non-reflective
# 400 ``invalid_request`` (``SyncControlRoute``), not with a VAL-001 422.
SYNC_REFUSED = [
    (
        "GET",
        f"{WORKSPACE}/sync-installations/{uuid4()}/activity/events?before_id={PAST_INT32}",
        None,
    ),
    (
        "POST",
        f"{WORKSPACE}/sync-authorizations",
        {
            "request_id": str(uuid4()),
            "application_id": "app",
            "public_name": "Name",
            "public_description": "{value}\u0000",
        },
    ),
    (
        "POST",
        f"{WORKSPACE}/sync-authorizations",
        {
            "request_id": str(uuid4()),
            "application_id": "app",
            "public_name": "Name",
            "public_usage_guide": "{value}\u0000",
        },
    ),
]


@pytest.mark.parametrize(("method", "url", "body"), SYNC_REFUSED)
def test_a_sync_value_the_database_cannot_take_is_refused_before_any_statement(
    client, session, monkeypatch, method, url, body
):
    monkeypatch.setattr(get_settings(), "sync_integrations_enabled", True)
    monkeypatch.setattr(get_settings(), "sync_observability_enabled", True)

    async def _principal():
        return object()

    app.dependency_overrides[sync_manager] = _principal
    app.dependency_overrides[sync_reader] = _principal
    value = f"refused{secrets.token_hex(6)}"
    with structlog.testing.capture_logs() as events:
        response = _request(client, method, url, body, value)

    assert response.status_code == 400, response.text
    assert response.json() == {"error": "invalid_request"}
    assert session.statements == []
    session.add.assert_not_called()
    assert value not in json.dumps(events, default=str)


def test_the_largest_sync_event_cursor_reaches_the_route(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_integrations_enabled", True)
    monkeypatch.setattr(get_settings(), "sync_observability_enabled", True)

    async def _principal():
        return object()

    app.dependency_overrides[sync_reader] = _principal
    response = client.get(
        f"{WORKSPACE}/sync-installations/{uuid4()}/activity/events?before_id={PAST_INT32 - 1}"
    )
    assert response.json() != {"error": "invalid_request"}


# cost_aggregation.py and admin_neural.py: the same filters on both cost
# routes (a NUL in ``user_id``; a ``to`` whose next day is past the last date
# there is, since the window ends the day after ``to``), and a NUL model name.
COST_WINDOW = "period=day&from=2026-01-01&to=2026-01-02"
COST_REFUSED = [
    f"{{route}}?{COST_WINDOW}&user_id={{value}}{NUL}",
    "{route}?period=day&from=9999-12-31&to=9999-12-31&_={value}",
]
COST_ROUTES = [f"{ADMIN}/cost-aggregation", f"{WORKSPACE}/cost-aggregation"]


@pytest.mark.parametrize("route", COST_ROUTES)
@pytest.mark.parametrize("url", COST_REFUSED)
def test_a_cost_filter_the_database_cannot_take_is_a_422_before_any_statement(
    client, session, monkeypatch, route, url
):
    monkeypatch.setattr(get_settings(), "enable_byok", True)
    monkeypatch.setattr(get_settings(), "enable_cost_display", True)
    value = f"refused{secrets.token_hex(6)}"
    with structlog.testing.capture_logs() as events:
        response = _request(client, "GET", url.replace("{route}", route), None, value)

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert value not in response.text
    assert session.statements == []
    assert value not in json.dumps(events, default=str)
    assert not any(event.get("event") == "database_error" for event in events)


def test_a_nul_model_name_is_refused_before_a_recalibration_is_queued(client, monkeypatch):
    import api.routes.admin_neural as admin_neural

    queued = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_neural, "enqueue_recalibration_dedup", queued)
    value = f"refused{secrets.token_hex(6)}"
    with structlog.testing.capture_logs() as events:
        response = client.post(f"{ADMIN}/neural/recalibrate?dimensions=8&model={value}{NUL}")

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    queued.assert_not_awaited()
    assert value not in json.dumps(events, default=str)
    # Any other model name is still queued.
    assert client.post(f"{ADMIN}/neural/recalibrate?dimensions=8&model=a%25b").status_code == 202
