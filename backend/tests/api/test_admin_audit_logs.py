"""Admin read of the audit log: gate, validation and what an entry holds (#2835).

``GET /api/v1/admin/audit-logs`` with a scripted session, so every
``SYSTEM_ADMIN_MODE`` and every refusal runs without Postgres. The same rules
run against real rows in ``test_admin_audit_logs_db.py``.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db

URL = "/api/v1/admin/audit-logs"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
USER_SESSION = {"user_id": "1002", "email": "user@example.com", "role": "user"}
OPERATOR = "ops@example.com"

LISTED = {
    "system_role_update",
    "system_admin_promote",
    "system_admin_demote",
    "workspace_slot_bonus_update",
    "referral_grant_revoke",
    "workspace_ownership_force_transfer",
    "workspace_ownership_transfer",
    "workspace_purged",
    "account_erasure",
    "context_restore",
    "bm25_drift_reveal_terms",
    "mcp_notice.created",
    "mcp_notice.withdrawn",
    "signup_blocked",
    "signup_admitted",
    "beta_invite.redeemed",
}
PLAIN = {
    "system_role_update",
    "system_admin_promote",
    "system_admin_demote",
    "workspace_slot_bonus_update",
    "referral_grant_revoke",
}
METADATA_KEYS = {
    "system_role_update": {"actor_user_id", "target_user_id", "old_role", "new_role"},
    "system_admin_promote": {"promoted_by", "is_initial_admin"},
    "system_admin_demote": {"demoted_by", "was_initial_admin"},
    "workspace_slot_bonus_update": {
        "actor_user_id",
        "target_user_id",
        "before_value",
        "after_value",
        "delta",
        "reason",
    },
    "referral_grant_revoke": {
        "actor_user_id",
        "referrer_user_id",
        "referred_user_id",
        "referrer_bonus_memories",
        "referred_bonus_memories",
        "reason",
    },
    "workspace_ownership_force_transfer": {
        "break_glass",
        "reason",
        "previous_owner_id",
        "new_owner_id",
        "ownership_epoch",
        "target_was_member",
        "self_transfer",
        "dual_control",
        "force_transfer_request_id",
        "initiated_by_user_id",
        "initiated_by_email",
        "approved_by_user_id",
    },
    "workspace_ownership_transfer": {"previous_owner_id", "new_owner_id", "ownership_epoch"},
    "workspace_purged": {
        "previous_owner_id",
        "soft_deleted",
        "contexts",
        "memories",
        "billing_contract_active",
    },
    "account_erasure": {"request_id", "is_self_service", "initiated_by", "reason_code"},
    "context_restore": {
        "workspace_id",
        "name",
        "renamed_from",
        "deleted_at",
        "deleted_by",
        "memories_restored",
        "memories_left_deleted",
        "files_restored",
        "file_bytes_restored",
    },
    "bm25_drift_reveal_terms": {
        "outcome",
        "context_id",
        "reason",
        "num_terms_resolved",
        "num_terms_total",
    },
    "mcp_notice.created": {"text_chars", "starts_at", "expires_at", "plans"},
    "mcp_notice.withdrawn": {"text_chars"},
    "signup_blocked": {"provider"},
    "signup_admitted": {"provider", "admission", "invitations", "invitation_count"},
    "beta_invite.redeemed": {"provider", "inviter_user_id", "allowlist_entry_id"},
}
ENTRY_KEYS = {
    "id",
    "created_at",
    "action",
    "user_id",
    "user_email",
    "resource",
    "old_value",
    "new_value",
    "metadata",
}
SELECTED = {
    "id",
    "created_at",
    "action",
    "user_id",
    "user_email",
    "resource",
    "old_value_hash",
    "new_value_hash",
    "user_metadata",
}


def _token() -> str:
    return "tok" + secrets.token_hex(12)


def _row(row_id: int = 7, action: str = "system_role_update", **overrides: Any) -> SimpleNamespace:
    """A row as the entity would come back: every column, tokens in the unanswered ones."""
    values: dict[str, Any] = {
        "id": row_id,
        "user_email": "admin@example.com",
        "user_id": "1001",
        "action": action,
        "resource": "user:local:alice",
        "old_value_hash": "user",
        "new_value_hash": "read_only",
        "user_metadata": {
            "actor_user_id": "1001",
            "target_user_id": "local:alice",
            "old_role": "user",
            "new_role": "read_only",
        },
        "ip_address": _token(),
        "user_agent": _token(),
        "created_at": datetime(2026, 11, 15, 9, 0, 0),  # naive UTC, as the column stores it
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return list(self._rows)


class ScriptedSession:
    """Answers the route's statement and keeps it for inspection."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a read route must not call session.{name}")


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch):
    def _apply(value: str, **values: str) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", value)
        defaults = {
            "system_admin_service_token": "",
            "system_admin_service_token_previous": "",
            "system_admin_service_allowed_cidrs": "",
        }
        defaults.update(values)
        for field, item in defaults.items():
            monkeypatch.setattr(settings, field, item)

    return _apply


@pytest.fixture
def session() -> ScriptedSession:
    return ScriptedSession([_row()])


@pytest.fixture
def client(session: ScriptedSession):
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as(principal: dict | None) -> None:
    if principal is None:
        app.dependency_overrides.pop(get_current_user_optional, None)
        return

    async def _session():
        return dict(principal)

    app.dependency_overrides[get_current_user_optional] = _session


@pytest.fixture
def admin(client, mode):
    mode("session")
    _as(ADMIN_SESSION)
    return client


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _where(statement: Any) -> str:
    return _sql(statement).split("WHERE")[1].split("ORDER BY")[0]


def _in_list(where: str) -> set[str]:
    inside = where.split("audit_logs.action IN (")[1].split(")")[0]
    return {part.strip().strip("'") for part in inside.split(",")}


# --- the allow-list ------------------------------------------------------------------


def test_the_allow_list_is_the_sixteen_actions() -> None:
    from api.routes import admin_audit_logs as module

    assert isinstance(module.AUDIT_LOG_ACTIONS, frozenset)
    assert module.AUDIT_LOG_ACTIONS == LISTED
    assert len(LISTED) == 16
    assert module.PLAIN_VALUE_ACTIONS == PLAIN
    assert {action: set(keys) for action, keys in module.AUDIT_METADATA_KEYS.items()} == (
        METADATA_KEYS
    )
    assert set(module.AUDIT_METADATA_KEYS) == module.AUDIT_LOG_ACTIONS
    assert "deleted_data_summary" not in module.AUDIT_METADATA_KEYS["account_erasure"]


def test_the_openapi_description_names_every_action_and_its_columns() -> None:
    operation = app.openapi()["paths"][URL]["get"]
    description = operation["description"]
    for action in LISTED:
        assert f"`{action}`" in description, action
    for column in ("`user_id`", "`user_email`", "`resource`"):
        assert column in description
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert set(parameters) == {
        "action",
        "user_id",
        "user_email",
        "resource",
        "from",
        "to",
        "limit",
        "before_id",
    }


# --- metadata: each listed key as stored ----------------------------------------------


def _metadata(action: str, stored: Any) -> Any:
    from api.routes.admin_audit_logs import audit_metadata_of

    return audit_metadata_of(action, stored)


@pytest.mark.parametrize("stored", [None, "text", 3, ["a"], True])
def test_metadata_that_is_not_an_object_is_null(stored: Any) -> None:
    assert _metadata("system_role_update", stored) is None


def test_metadata_keeps_the_listed_keys_with_their_stored_scalars() -> None:
    stored = {
        "actor_user_id": "1001",
        "target_user_id": "local:alice",
        "before_value": 0,
        "after_value": 2.5,
        "delta": -3,
        "reason": None,
    }
    assert _metadata("workspace_slot_bonus_update", stored) == stored
    flags = {"promoted_by": "admin@example.com", "is_initial_admin": False}
    assert _metadata("system_admin_promote", flags) == flags
    assert _metadata("signup_blocked", {}) == {}


@pytest.mark.parametrize("action", sorted(LISTED))
def test_metadata_drops_every_key_that_is_not_listed_for_the_action(action: str) -> None:
    token = _token()
    keys = METADATA_KEYS[action]
    listed = {key: f"value-{index}" for index, key in enumerate(sorted(keys))}
    # a key another action lists, and this one does not
    of_another_action = "provider" if "reason" in keys else "reason"
    assert of_another_action not in keys
    stored = {
        **listed,
        "added_later": token,
        "ip_address": token,
        "nested": {"inner": token},
        "items": [token],
        "deleted_data_summary": {"note": token},
        of_another_action: token,
    }
    answered = _metadata(action, stored)
    assert answered == listed
    assert token not in repr(answered)


@pytest.mark.parametrize("action", sorted(LISTED))
def test_metadata_drops_a_listed_key_that_holds_an_object_or_a_list(action: str) -> None:
    token = _token()
    stored: dict[str, Any] = {}
    for index, key in enumerate(sorted(METADATA_KEYS[action])):
        stored[key] = {"inner": token} if index % 2 else [token, {"inner": token}]
    answered = _metadata(action, stored)
    assert token not in repr(answered)
    # ``invitations`` stays a list (of nothing: its entries held no listed string)
    assert answered == ({"invitations": [{}]} if action == "signup_admitted" else {})


def test_metadata_plans_is_a_list_of_strings_or_dropped() -> None:
    assert _metadata("mcp_notice.created", {"text_chars": 12, "plans": ["free", "pro"]}) == {
        "text_chars": 12,
        "plans": ["free", "pro"],
    }
    assert _metadata("mcp_notice.created", {"plans": None}) == {"plans": None}
    token = _token()
    for value in (["free", 3], [["free"]], [{"name": token}], {"free": token}):
        assert _metadata("mcp_notice.created", {"text_chars": 12, "plans": value}) == {
            "text_chars": 12
        }
    # only the action that lists the key
    assert _metadata("mcp_notice.withdrawn", {"text_chars": 12, "plans": ["free"]}) == {
        "text_chars": 12
    }
    # a list under another listed key is not ``plans``
    assert _metadata("mcp_notice.created", {"starts_at": ["free"]}) == {}


def test_metadata_invitations_are_reduced_to_three_string_keys() -> None:
    token = _token()
    stored = {
        "provider": "google",
        "admission": "workspace_invitation",
        "invitation_count": 2,
        "invitations": [
            {"id": "inv_1", "workspace_id": "ws-1", "invited_by": "local:bob", "email": token},
            {"id": 17, "workspace_id": {"id": token}, "invited_by": None, "note": [token]},
            token,
            [token],
        ],
    }
    assert _metadata("signup_admitted", stored) == {
        "provider": "google",
        "admission": "workspace_invitation",
        "invitation_count": 2,
        "invitations": [
            {"id": "inv_1", "workspace_id": "ws-1", "invited_by": "local:bob"},
            {},
        ],
    }
    assert _metadata("signup_admitted", {"invitations": {"id": token}}) == {}
    # only the action that lists the key
    assert _metadata("signup_blocked", {"invitations": [{"id": "inv_1"}]}) == {}


def test_metadata_of_an_action_outside_the_list_is_empty() -> None:
    assert _metadata("terms.accepted", {"provider": "google", "version": "3"}) == {}


# --- the gate ------------------------------------------------------------------------


def test_session_admin_is_answered(client, mode, session) -> None:
    mode("session")
    _as(ADMIN_SESSION)
    response = client.get(URL)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"entries", "next_before_id"}


def test_session_refusals_read_nothing(client, mode, session) -> None:
    mode("session")
    _as(None)
    assert client.get(URL).status_code == 401
    _as(USER_SESSION)
    assert client.get(URL).status_code == 403
    assert session.statements == []


def test_off_is_an_unknown_path(client, mode, session) -> None:
    mode("off")
    _as(ADMIN_SESSION)
    response = client.get(URL)
    assert response.status_code == 404
    assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
    assert session.statements == []


def test_service_operator_is_answered_without_a_users_lookup(client, mode, session) -> None:
    token = secrets.token_hex(32)
    mode("service", system_admin_service_token=token)
    _as(None)
    headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    with capture_logs() as logs:
        response = client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    [statement] = session.statements
    assert " users" not in _sql(statement)
    [entry] = [e for e in logs if e["event"] == "audit_logs_listed"]
    assert entry["admin_user"] == f"operator:{OPERATOR}"
    # as every gated route: an admin session without the credential is refused
    _as(ADMIN_SESSION)
    refused = client.get(URL)
    assert refused.status_code == 401
    assert refused.json()["error"] == "SYSADMIN-002"
    assert len(session.statements) == 1


# --- validation: 422, never clamped -----------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "action=terms.accepted",
        "action=user.signed_up",
        "action=role_assign",
        "action=capacity_purge",
        "action=public_bound_key_created",
        "action=",
        "action=SYSTEM_ROLE_UPDATE",
        "action=system_role_update&action=role_assign",
        "action=system_role_update,account_erasure",
        "limit=0",
        "limit=101",
        "limit=abc",
        "before_id=0",
        "before_id=-5",
        "before_id=abc",
        "user_id=",
        "user_id=" + "a" * 256,
        "user_email=",
        "user_email=" + "a" * 256,
        "resource=",
        "resource=" + "a" * 256,
        "from=2026-11-15T09:00:00",
        "from=2026-11-15",
        "from=1763197200",
        "from=yesterday",
        "from=",
        "to=2026-11-15T09:00:00",
        "to=1763197200",
        "to=tomorrow",
        "from=2026-11-15T09:00:01Z&to=2026-11-15T09:00:00Z",
        "from=2026-11-15T09:00:00%2B09:00&to=2026-11-14T23:59:59Z",
    ],
    ids=lambda q: q[:48],
)
def test_a_bad_query_value_is_422(admin, session, query) -> None:
    response = admin.get(f"{URL}?{query}")
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert session.statements == []


def test_a_refused_value_is_not_echoed(admin) -> None:
    token = _token()
    for query in (f"action={token}", f"from={token}", f"to={token}", f"before_id={token}"):
        response = admin.get(f"{URL}?{query}")
        assert response.status_code == 422
        assert token not in response.text


def test_the_limits_at_their_bounds_are_accepted(admin, session) -> None:
    assert admin.get(f"{URL}?limit=1&before_id=1").status_code == 200
    assert admin.get(f"{URL}?limit=100").status_code == 200
    assert admin.get(f"{URL}?user_id={'a' * 255}").status_code == 200
    # an empty range is a range
    same = "from=2026-11-15T09:00:00Z&to=2026-11-15T09:00:00Z"
    assert admin.get(f"{URL}?{same}").status_code == 200
    statements = [_sql(statement) for statement in session.statements]
    assert "LIMIT 2" in statements[0] and "audit_logs.id < 1" in statements[0]
    assert "LIMIT 101" in statements[1]
    # the default
    session.statements.clear()
    assert admin.get(URL).status_code == 200
    assert "LIMIT 51" in _sql(session.statements[0])


# --- the statement -----------------------------------------------------------------


def test_the_statement_selects_named_columns_and_always_the_allow_list(admin, session) -> None:
    assert admin.get(URL).status_code == 200
    [statement] = session.statements
    names = {column.name for column in statement.selected_columns}
    assert names == SELECTED
    sql = _sql(statement)
    assert "ip_address" not in sql and "user_agent" not in sql
    where = _where(statement)
    # without ``action``: every listed action, and only those
    assert _in_list(where) == LISTED
    assert " AND " not in where and " OR " not in where
    assert sql.split("ORDER BY")[1].split("LIMIT")[0].split() == ["audit_logs.id", "DESC"]


def test_asked_actions_narrow_the_allow_list(admin, session) -> None:
    query = "action=signup_blocked&action=mcp_notice.created&action=signup_blocked"
    assert admin.get(f"{URL}?{query}").status_code == 200
    where = _where(session.statements[0])
    assert _in_list(where) == {"signup_blocked", "mcp_notice.created"}
    assert where.count("signup_blocked") == 1  # duplicates are merged
    assert where.count("audit_logs.action") == 1


def test_the_filters_are_exact_and_combine(admin, session) -> None:
    actor, address, resource = "local:" + _token(), _token() + "_", "user:" + _token()
    response = admin.get(
        URL,
        params={
            "action": "system_role_update",
            "user_id": actor,
            "user_email": " " + address,
            "resource": resource,
            "from": "2026-11-15T09:00:00+09:00",
            "to": "2026-11-15T12:30:00.500000Z",
            "before_id": 900,
            "limit": 10,
        },
    )
    assert response.status_code == 200, response.text
    where = _where(session.statements[0])
    assert _in_list(where) == {"system_role_update"}
    # D4: the stored value, untrimmed, with no wildcard reading
    assert f"audit_logs.user_id = '{actor}'" in where
    assert f"audit_logs.user_email = ' {address}'" in where
    assert f"audit_logs.resource = '{resource}'" in where
    assert "LIKE" not in where and "lower(" not in where
    # D5: ``from`` inclusive, ``to`` exclusive, both as naive UTC
    assert "audit_logs.created_at >= '2026-11-15 00:00:00'" in where
    assert "audit_logs.created_at < '2026-11-15 12:30:00.500000'" in where
    assert "audit_logs.id < 900" in where
    assert where.count(" AND ") == 6 and " OR " not in where


@pytest.mark.parametrize(
    "query",
    [
        "user_id=x",
        "user_email=x",
        "resource=x",
        "from=2026-11-15T09:00:00Z",
        "to=2026-11-15T09:00:00Z",
        "before_id=5",
    ],
)
def test_each_filter_alone_still_carries_the_allow_list(admin, session, query) -> None:
    assert admin.get(f"{URL}?{query}").status_code == 200
    where = _where(session.statements[0])
    assert _in_list(where) == LISTED
    assert where.count(" AND ") == 1


# --- paging ------------------------------------------------------------------------


def test_next_before_id_is_the_last_answered_id_when_one_more_row_exists(admin, session) -> None:
    session.rows = [_row(row_id) for row_id in (9, 8, 7)]  # limit + 1 rows came back
    body = admin.get(f"{URL}?limit=2").json()
    assert [entry["id"] for entry in body["entries"]] == [9, 8]
    assert body["next_before_id"] == 8


def test_next_before_id_is_null_on_the_last_page(admin, session) -> None:
    session.rows = [_row(row_id) for row_id in (9, 8)]
    body = admin.get(f"{URL}?limit=2").json()
    assert [entry["id"] for entry in body["entries"]] == [9, 8]
    assert body["next_before_id"] is None
    session.rows = []
    assert admin.get(URL).json() == {"entries": [], "next_before_id": None}


# --- what an entry holds -----------------------------------------------------------


def test_an_entry_has_the_named_fields_only(admin, session) -> None:
    row = session.rows[0]
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    [entry] = response.json()["entries"]
    assert set(entry) == ENTRY_KEYS
    assert entry == {
        "id": 7,
        "created_at": "2026-11-15T09:00:00Z",
        "action": "system_role_update",
        "user_id": "1001",
        "user_email": "admin@example.com",
        "resource": "user:local:alice",
        "old_value": "user",
        "new_value": "read_only",
        "metadata": {
            "actor_user_id": "1001",
            "target_user_id": "local:alice",
            "old_role": "user",
            "new_role": "read_only",
        },
    }
    for token in (row.ip_address, row.user_agent):
        assert token not in response.text
    for name in ("ip_address", "user_agent", "_hash", "user_metadata"):
        assert name not in response.text


@pytest.mark.parametrize("action", sorted(LISTED))
def test_the_value_columns_are_answered_for_the_plain_value_actions_only(
    admin, session, action
) -> None:
    old, new = _token(), _token()
    session.rows = [_row(action=action, old_value_hash=old, new_value_hash=new)]
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    [entry] = response.json()["entries"]
    if action in PLAIN:
        assert (entry["old_value"], entry["new_value"]) == (old, new)
    else:
        assert (entry["old_value"], entry["new_value"]) == (None, None)
        assert old not in response.text and new not in response.text


def test_stored_metadata_is_answered_through_the_key_list(admin, session) -> None:
    token = _token()
    session.rows = [
        _row(
            action="account_erasure",
            user_metadata={
                "request_id": "r-1",
                "is_self_service": False,
                "initiated_by": "1001",
                "reason_code": "legal_order",
                "deleted_data_summary": {"note": token, "postgres": {"memories": 3}},
                "added_later": token,
            },
        ),
        _row(action="signup_blocked", user_metadata=None),
    ]
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    first, second = response.json()["entries"]
    assert first["metadata"] == {
        "request_id": "r-1",
        "is_self_service": False,
        "initiated_by": "1001",
        "reason_code": "legal_order",
    }
    assert second["metadata"] is None
    assert token not in response.text
    assert "deleted_data_summary" not in response.text


# --- the log line ------------------------------------------------------------------


def test_the_log_line_names_the_caller_and_no_filter_value(admin, session) -> None:
    actor, address, resource = "local:" + _token(), _token() + "@example.com", "user:" + _token()
    with capture_logs() as logs:
        response = admin.get(
            URL,
            params={
                "action": ["signup_blocked", "account_erasure", "signup_blocked"],
                "user_id": actor,
                "user_email": address,
                "resource": resource,
                "to": "2026-11-15T12:30:00Z",
                "limit": 10,
            },
        )
    assert response.status_code == 200, response.text
    [entry] = [e for e in logs if e["event"] == "audit_logs_listed"]
    assert entry == {
        "event": "audit_logs_listed",
        "log_level": "info",
        "admin_user": "1001",
        "count": 1,
        "limit": 10,
        "actions": ["account_erasure", "signup_blocked"],
        "user_id_filter": True,
        "user_email_filter": True,
        "resource_filter": True,
        "from_set": False,
        "to_set": True,
        "before_id_set": False,
    }
    row = session.rows[0]
    for value in (actor, address, resource, row.resource, row.ip_address, row.user_agent):
        assert value not in repr(logs)

    with capture_logs() as logs:
        assert admin.get(URL).status_code == 200
    [entry] = [e for e in logs if e["event"] == "audit_logs_listed"]
    assert entry["actions"] is None
    assert [entry[key] for key in entry if key.endswith(("_filter", "_set"))] == [False] * 6
