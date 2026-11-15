"""A value the database would refuse answers 422 before any statement (#2828, #2843, #2833, #2832, #2846).

No stored text holds a NUL and an ``OFFSET`` is a 64-bit integer. The routes
of these issues refuse such a value in the parameter's declaration, so the
request never reaches a statement and no log line holds the value.

The largest accepted values are run against Postgres in
``tests/integration/test_admin_operator_reads_refused_values_db.py``.
"""

from __future__ import annotations

import json
import secrets

import pytest
import structlog
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from tests.api.test_admin_role_protections import account, role_change_session

USERS = "/api/v1/admin/users"
DELETED_CONTEXTS = "/api/v1/admin/contexts/deleted"
EMBEDDING_FAILURES = "/api/v1/admin/embedding/failures"
FORCE_TRANSFER_REQUESTS = "/api/v1/admin/force-transfer-requests"
SYSTEM_ADMINS = "/api/v1/admin/system-admins"
PAST_OFFSET = 2**63

REFUSED = [
    ("GET", f"{USERS}?user_id=a%00b", None),
    ("GET", f"{USERS}?user_id=%00", None),
    # The parameters the user list already had, bounded with it (#2843).
    ("GET", f"{USERS}?search=a%00b", None),
    ("GET", f"{USERS}?role=a%00b", None),
    ("GET", f"{USERS}?plan=%00", None),
    ("GET", f"{USERS}?invited_by=a%00b", None),
    ("GET", f"{USERS}?offset={PAST_OFFSET}", None),
    ("GET", f"{USERS}?workspace_id=not-a-uuid", None),
    ("GET", f"{USERS}?workspace_id=a%00b", None),
    ("GET", f"{DELETED_CONTEXTS}?created_by=a%00b", None),
    ("GET", f"{DELETED_CONTEXTS}?deleted_by=%00", None),
    ("GET", f"{DELETED_CONTEXTS}?offset={PAST_OFFSET}", None),
    ("GET", f"{EMBEDDING_FAILURES}?offset={PAST_OFFSET}", None),
    ("GET", f"{FORCE_TRANSFER_REQUESTS}?offset={PAST_OFFSET}", None),
    ("PUT", f"{USERS}/a%00b/role", {"role": "user"}),
    ("PUT", f"{USERS}/a%00b/role", {"role": "admin"}),
    ("DELETE", f"{SYSTEM_ADMINS}/a%00b", None),
]


@pytest.fixture
def session():
    return role_change_session(account("1001", "admin"), account("2002", "admin"))


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _db():
        yield session

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(("method", "url", "body"), REFUSED, ids=lambda value: str(value)[-40:])
def test_a_value_the_database_would_refuse_is_a_422_before_any_statement(
    client, session, method, url, body
):
    response = client.request(method, url, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert session.statements == []
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        f"{USERS}?user_id={{value}}%00",
        f"{USERS}?search={{value}}%00",
        f"{USERS}?role={{value}}%00",
        f"{USERS}?plan={{value}}%00",
        f"{USERS}?invited_by={{value}}%00",
        f"{USERS}?workspace_id={{value}}",
        f"{USERS}?search={{value}}&offset={PAST_OFFSET}",
        f"{DELETED_CONTEXTS}?created_by={{value}}%00",
        f"{DELETED_CONTEXTS}?deleted_by={{value}}%00",
        f"{DELETED_CONTEXTS}?created_by={{value}}&offset={PAST_OFFSET}",
    ],
)
def test_a_refused_filter_value_is_not_logged(client, session, url):
    value = f"refused-{secrets.token_hex(8)}"
    with structlog.testing.capture_logs() as events:
        response = client.get(url.format(value=value))
    assert response.status_code == 422, response.text
    assert value not in json.dumps(events, default=str)
    assert session.statements == []


@pytest.mark.parametrize(
    ("method", "url"),
    [("PUT", f"{USERS}/{{value}}%00/role"), ("DELETE", f"{SYSTEM_ADMINS}/{{value}}%00")],
)
def test_a_refused_account_id_is_logged_as_the_request_path_only(client, session, method, url):
    # The id is part of the URL, so the request path names it, as an access
    # log does; no other field of a log line holds it.
    value = f"refused-{secrets.token_hex(8)}"
    with structlog.testing.capture_logs() as events:
        response = client.request(method, url.format(value=value), json={"role": "user"})
    assert response.status_code == 422, response.text
    beside_the_path = [
        {key: field for key, field in event.items() if key != "path"} for event in events
    ]
    assert value not in json.dumps(beside_the_path, default=str)
    assert session.statements == []


@pytest.mark.parametrize(
    "url",
    [
        f"{USERS}?user_id=a%0Ab%25%20c",
        f"{USERS}?search=a%0Ab%25%20c&role=a%0Ab&plan=a%0Ab&invited_by=a%0Ab%25%20c",
        f"{USERS}?offset={PAST_OFFSET - 1}",
        f"{USERS}?workspace_id=11111111-2222-4333-8444-555555555555",
        # An empty value is no filter, as it was before the id was typed.
        f"{USERS}?workspace_id=&role=&plan=&search=",
        f"{DELETED_CONTEXTS}?created_by=a%0Ab%25%20c&deleted_by=system:capacity-purge",
        f"{DELETED_CONTEXTS}?offset={PAST_OFFSET - 1}",
        f"{EMBEDDING_FAILURES}?offset={PAST_OFFSET - 1}",
        f"{FORCE_TRANSFER_REQUESTS}?offset={PAST_OFFSET - 1}",
    ],
)
def test_every_other_character_and_the_largest_offset_reach_the_statement(client, session, url):
    client.get(url)
    assert session.statements != []


def test_an_account_id_with_other_characters_reaches_the_shared_check(client, session):
    # A stored id may hold a line break, a percent sign or a space.
    put = client.put(f"{USERS}/a%0Ab%25%20c/role", json={"role": "user"})
    demote = client.delete(f"{SYSTEM_ADMINS}/a%0Ab%25%20c")
    assert (put.status_code, demote.status_code) == (404, 404)
    assert len(session.statements) == 2
