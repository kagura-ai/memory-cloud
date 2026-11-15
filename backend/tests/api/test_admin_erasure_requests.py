"""Admin reads of erasure requests: gate, validation and what an item holds (#2834).

``GET /api/v1/admin/erasure-requests`` and ``.../{request_id}`` with a scripted
session, so every ``SYSTEM_ADMIN_MODE`` and every refusal runs without
Postgres. The rows the scripted session hands back carry a unique token in
every column the route does not answer; the same tokens are seeded into real
rows in ``test_admin_erasure_requests_db.py``.
"""

from __future__ import annotations

import ast
import inspect
import secrets
import typing
import uuid
from datetime import UTC, datetime
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
from models import erasure as erasure_models
from models.erasure import (
    FAILURE_AFTER_POSTGRES_COMMIT,
    INTERRUPTED_AFTER_POSTGRES_COMMIT,
    VALID_STATUSES,
)

URL = "/api/v1/admin/erasure-requests"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
USER_SESSION = {"user_id": "1002", "email": "user@example.com", "role": "user"}
OPERATOR = "ops@example.com"

ITEM_KEYS = {
    "request_id",
    "user_id",
    "status",
    "is_self_service",
    "reason_code",
    "initiated_by",
    "requested_at",
    "confirmed_at",
    "scheduled_for",
    "started_at",
    "completed_at",
    "cancelled_at",
    "retry",
    "failure_kind",
}
RETRY_KEYS = {"attempts", "step", "error_class", "last_attempt_at", "embeddings_requeued"}
NEVER_SELECTED = {
    "user_email_hash",
    "reason_detail",
    "confirm_token_hash",
    "ip_address",
    "user_agent",
}


def _token() -> str:
    return "tok" + secrets.token_hex(12)


def _row(**overrides: Any) -> SimpleNamespace:
    """A row as the entity would come back: every column, tokens in the unanswered ones."""
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": "local:subject-1",
        "user_email_hash": _token(),
        "initiated_by": "1001",
        "is_self_service": False,
        "reason_code": "legal_order",
        "reason_detail": _token(),
        "status": "failed",
        "confirm_token_hash": _token(),
        "requested_at": datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
        "confirmed_at": None,
        "scheduled_for": None,
        "started_at": datetime(2026, 11, 14, 9, 0, 1, tzinfo=UTC),
        "completed_at": None,
        "cancelled_at": None,
        "failure_reason": f"vector_store_unavailable after 4 attempts: {_token()}",
        "deleted_data_summary": {
            "retry": {
                "attempts": 4,
                "step": "vector_store",
                "error_class": "ResponseHandlingException",
                "last_attempt_at": "2026-11-14T12:00:00Z",
            },
            "note": _token(),
        },
        "ip_address": _token(),
        "user_agent": _token(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _unanswered_tokens(row: SimpleNamespace) -> list[str]:
    return [
        row.user_email_hash,
        row.reason_detail,
        row.confirm_token_hash,
        row.ip_address,
        row.user_agent,
        row.failure_reason.rsplit(" ", 1)[-1],
        row.deleted_data_summary["note"],
    ]


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return list(self._rows)

    def one_or_none(self) -> SimpleNamespace | None:
        return self._rows[0] if self._rows else None


class ScriptedSession:
    """Answers the route's statements and keeps them for inspection."""

    def __init__(self, rows: list[SimpleNamespace], total: int | None = None) -> None:
        self.rows = rows
        self.total = len(rows) if total is None else total
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return self.total

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


# --- failure_kind: a closed set, a total function ---------------------------------


def test_failure_kind_of_maps_every_writer_to_a_fixed_token() -> None:
    from cli.reprocess_erasures import REPAIRED_REASON

    kind = erasure_models.failure_kind_of
    cases = {
        f"{FAILURE_AFTER_POSTGRES_COMMIT}redis: ConnectionError": "after_postgres_commit",
        INTERRUPTED_AFTER_POSTGRES_COMMIT: "after_postgres_commit",
        FAILURE_AFTER_POSTGRES_COMMIT + REPAIRED_REASON: "after_postgres_commit",
        "billing_contract_active: workspace_ids=a,b. Cancel the subscription": (
            "billing_contract_active"
        ),
        "workspace_transfer_required: workspace_id=w member_count=3. Promote": (
            "workspace_transfer_required"
        ),
        "vector_store_unavailable after 4 attempts: timed out": "vector_store_unavailable",
        "lock_conflict after 4 attempts: postgres: LockNotAvailable (another": "lock_conflict",
    }
    for reason, expected in cases.items():
        assert kind("failed", reason) == expected, reason


def test_failure_kind_of_other_and_null() -> None:
    kind = erasure_models.failure_kind_of
    # N3: no prefix matches, or no reason at all: ``other``.
    assert kind("failed", "duplicate key value violates unique constraint") == "other"
    assert kind("failed", None) == "other"
    assert kind("failed", "") == "other"
    # a prefix inside the text, or without its separator, is not a match
    assert kind("failed", "x billing_contract_active: workspace_ids=a") == "other"
    assert kind("failed", "lock_conflict") == "other"
    # N3: only a failed request has a kind, whatever the column holds
    for status in sorted(VALID_STATUSES - {"failed"}):
        assert kind(status, "vector_store_unavailable after 4 attempts: x") is None, status
        assert kind(status, None) is None


def test_failure_kind_literal_is_the_closed_set() -> None:
    assert set(typing.get_args(erasure_models.FailureKind)) == {
        "after_postgres_commit",
        "billing_contract_active",
        "workspace_transfer_required",
        "vector_store_unavailable",
        "lock_conflict",
        "other",
    }


def test_the_writers_use_the_prefix_constants() -> None:
    """D7: the service writes each fixed-form reason through the constant the classifier reads."""
    from services import account_erasure_service as service

    assert erasure_models.FAILURE_PREFIX_BILLING_CONTRACT == "billing_contract_active: "
    assert erasure_models.FAILURE_PREFIX_WORKSPACE_TRANSFER == "workspace_transfer_required: "
    assert erasure_models.FAILURE_PREFIX_VECTOR_STORE == "vector_store_unavailable after "
    assert erasure_models.FAILURE_PREFIX_LOCK_CONFLICT == "lock_conflict after "
    assert service.LOCK_CONFLICT_GAVE_UP is erasure_models.FAILURE_PREFIX_LOCK_CONFLICT

    prefixes = (
        "billing_contract_active",
        "workspace_transfer_required",
        "vector_store_unavailable after",
        "lock_conflict after",
    )
    names = {
        "FAILURE_PREFIX_BILLING_CONTRACT",
        "FAILURE_PREFIX_WORKSPACE_TRANSFER",
        "FAILURE_PREFIX_VECTOR_STORE",
        "LOCK_CONFLICT_GAVE_UP",
    }
    tree = ast.parse(inspect.getsource(service))
    inline: list[str] = []
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or not node.values:
            continue
        first = node.values[0]
        if isinstance(first, ast.Constant) and str(first.value).startswith(prefixes):
            inline.append(str(first.value))
        if (
            isinstance(first, ast.FormattedValue)
            and isinstance(first.value, ast.Name)
            and first.value.id in names
        ):
            used.add(first.value.id)
    assert inline == []
    assert used == names


# --- retry: a named, shape-checked block -------------------------------------------


def _retry(summary: Any) -> dict | None:
    from api.routes.admin_erasure_requests import erasure_retry_of

    block = erasure_retry_of(summary)
    return None if block is None else block.model_dump(mode="json")


def test_retry_is_null_without_a_block() -> None:
    assert _retry(None) is None
    assert _retry({}) is None
    assert _retry({"postgres": {"memories": 3}}) is None
    assert _retry({"retry": "text"}) is None
    assert _retry({"retry": ["a"]}) is None
    assert _retry("retry") is None


def test_retry_answers_the_five_keys_and_nothing_else() -> None:
    token = _token()
    block = _retry(
        {
            "retry": {
                "attempts": 2,
                "step": "vector_store",
                "error_class": "ResponseHandlingException",
                "last_attempt_at": "2026-11-14T12:00:00Z",
                "embeddings_requeued": 17,
                "requeued_at": "2026-11-14T12:00:01Z",
                "extra": token,
            },
            "note": token,
        }
    )
    assert block == {
        "attempts": 2,
        "step": "vector_store",
        "error_class": "ResponseHandlingException",
        "last_attempt_at": "2026-11-14T12:00:00Z",
        "embeddings_requeued": 17,
    }


def test_retry_of_a_request_whose_first_step_never_failed() -> None:
    assert _retry({"retry": {"attempts": 0}}) == {
        "attempts": 0,
        "step": None,
        "error_class": None,
        "last_attempt_at": None,
        "embeddings_requeued": None,
    }


@pytest.mark.parametrize(
    "value", [True, False, -1, 1.5, "3", None, [3], {"n": 3}], ids=lambda v: repr(v)
)
def test_retry_counts_are_non_negative_integers_or_null(value: Any) -> None:
    block = _retry({"retry": {"attempts": value, "embeddings_requeued": value}})
    assert block is not None
    assert block["attempts"] is None
    assert block["embeddings_requeued"] is None


@pytest.mark.parametrize(
    "value",
    [
        "has spaces",
        "tok with spaces",
        "user@example.com",
        "local:alice",
        "9starts_with_a_digit",
        "a" * 65,
        "",
        "Name\n",
        "dotted.Name",
        7,
        None,
        ["Name"],
        {"k": "Name"},
    ],
    ids=lambda v: repr(v)[:24],
)
def test_retry_step_and_error_class_are_identifiers_or_null(value: Any) -> None:
    block = _retry({"retry": {"attempts": 1, "step": value, "error_class": value}})
    assert block is not None
    assert block["step"] is None
    assert block["error_class"] is None


def test_retry_accepts_an_identifier_of_64_characters() -> None:
    name = "A" + "b" * 63
    block = _retry({"retry": {"step": name, "error_class": "_Private9"}})
    assert block is not None
    assert (block["step"], block["error_class"]) == (name, "_Private9")


@pytest.mark.parametrize(
    "value", ["yesterday", "", "2026-13-40T00:00:00Z", 1731585600, None, ["2026-11-14"]]
)
def test_retry_last_attempt_at_is_a_timestamp_or_null(value: Any) -> None:
    block = _retry({"retry": {"attempts": 1, "last_attempt_at": value}})
    assert block is not None
    assert block["last_attempt_at"] is None


def test_retry_last_attempt_at_keeps_its_zone() -> None:
    block = _retry({"retry": {"last_attempt_at": "2026-11-14T21:00:00.250000+09:00"}})
    assert block is not None
    assert block["last_attempt_at"] == "2026-11-14T21:00:00.250000+09:00"


# --- the gate ------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/00000000-0000-4000-8000-000000000001"])
def test_session_admin_is_answered(client, mode, session, suffix) -> None:
    mode("session")
    _as(ADMIN_SESSION)
    response = client.get(URL + suffix)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("suffix", ["", "/00000000-0000-4000-8000-000000000001"])
def test_session_refusals_read_nothing(client, mode, session, suffix) -> None:
    mode("session")
    _as(None)
    assert client.get(URL + suffix).status_code == 401
    _as(USER_SESSION)
    assert client.get(URL + suffix).status_code == 403
    assert session.statements == []


@pytest.mark.parametrize("suffix", ["", "/00000000-0000-4000-8000-000000000001"])
def test_off_is_an_unknown_path(client, mode, session, suffix) -> None:
    mode("off")
    _as(ADMIN_SESSION)
    response = client.get(URL + suffix)
    assert response.status_code == 404
    assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
    assert session.statements == []


@pytest.mark.parametrize("suffix", ["", "/00000000-0000-4000-8000-000000000001"])
def test_service_operator_is_answered_without_a_users_lookup(client, mode, session, suffix) -> None:
    token = secrets.token_hex(32)
    mode("service", system_admin_service_token=token)
    _as(None)
    headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    with capture_logs() as logs:
        response = client.get(URL + suffix, headers=headers)
    assert response.status_code == 200, response.text
    # one table only: the caller is never looked up
    for statement in session.statements:
        sql = _sql(statement)
        assert "erasure_requests" in sql
        assert "users" not in sql.replace("erasure_requests", "")
    [entry] = [e for e in logs if e["event"] in {"erasure_requests_listed", "erasure_request_read"}]
    assert entry["admin_user"] == f"operator:{OPERATOR}"
    # without the credential an admin session is refused
    _as(ADMIN_SESSION)
    before = len(session.statements)
    assert client.get(URL + suffix).status_code == 401
    assert len(session.statements) == before


# --- validation: 422, never clamped ------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "status=done",
        "status=FAILED",
        "status=",
        "limit=0",
        "limit=201",
        "limit=abc",
        "offset=-1",
        # past what the database takes as an OFFSET
        "offset=9223372036854775808",
        "offset=99999999999999999999",
        "user_id=",
        "user_id=" + "a" * 256,
        # a NUL is in no stored value and is refused before the statement
        "user_id=local:a%00b",
        "user_id=%00",
        "is_self_service=maybe",
    ],
    ids=lambda q: q[:40],
)
def test_a_bad_query_value_is_422(admin, session, query) -> None:
    response = admin.get(f"{URL}?{query}")
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert session.statements == []


def test_a_refused_filter_value_is_not_echoed_or_logged(admin, session) -> None:
    address = _token() + "@example.com"
    with capture_logs() as logs:
        response = admin.get(f"{URL}?user_id={address}%00&offset=9223372036854775808")
    assert response.status_code == 422, response.text
    assert address not in response.text
    assert address not in repr(logs)
    assert session.statements == []


def test_the_status_filter_takes_exactly_the_six_statuses(admin) -> None:
    from api.routes.admin_erasure_requests import ErasureStatus

    assert set(typing.get_args(ErasureStatus)) == VALID_STATUSES
    for status in sorted(VALID_STATUSES):
        assert admin.get(f"{URL}?status={status}").status_code == 200, status


def test_the_limits_at_their_bounds_are_accepted(admin) -> None:
    assert admin.get(f"{URL}?limit=1&offset=0").status_code == 200
    body = admin.get(f"{URL}?limit=200&offset=7").json()
    assert (body["limit"], body["offset"]) == (200, 7)
    default = admin.get(URL).json()
    assert (default["limit"], default["offset"]) == (50, 0)
    assert admin.get(f"{URL}?offset=9223372036854775807").status_code == 200
    assert admin.get(f"{URL}?user_id={'a' * 255}").status_code == 200
    # a stored id may hold any other character: a line break, a percent sign, a space
    assert admin.get(f"{URL}?user_id=a%0Ab%25%20c").status_code == 200


def test_an_id_that_is_not_a_uuid_is_422(admin, session) -> None:
    response = admin.get(f"{URL}/not-a-uuid")
    assert response.status_code == 422
    assert session.statements == []


def test_an_unknown_id_is_404(admin, session) -> None:
    session.rows = []
    response = admin.get(f"{URL}/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"] == "RES-001"


# --- what an item holds ------------------------------------------------------------


def test_list_and_read_answer_the_named_fields_only(admin, session) -> None:
    row = session.rows[0]
    listed = admin.get(URL)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    [item] = body["items"]
    one = admin.get(f"{URL}/{row.id}")
    assert one.status_code == 200, one.text
    assert one.json() == item
    # N5: the key sets are exact
    assert set(item) == ITEM_KEYS
    assert set(item["retry"]) == RETRY_KEYS
    assert item == {
        "request_id": str(row.id),
        "user_id": "local:subject-1",
        "status": "failed",
        "is_self_service": False,
        "reason_code": "legal_order",
        "initiated_by": "1001",
        "requested_at": "2026-11-14T09:00:00Z",
        "confirmed_at": None,
        "scheduled_for": None,
        "started_at": "2026-11-14T09:00:01Z",
        "completed_at": None,
        "cancelled_at": None,
        "retry": {
            "attempts": 4,
            "step": "vector_store",
            "error_class": "ResponseHandlingException",
            "last_attempt_at": "2026-11-14T12:00:00Z",
            "embeddings_requeued": None,
        },
        "failure_kind": "vector_store_unavailable",
    }
    for response in (listed, one):
        for token in _unanswered_tokens(row):
            assert token not in response.text
        assert "failure_reason" not in response.text
        assert "deleted_data_summary" not in response.text


def test_an_exception_text_naming_the_subject_answers_only_its_kind(admin, session) -> None:
    subject_id, address = "local:" + _token(), _token() + "@example.com"
    session.rows = [
        _row(
            user_id=subject_id,
            failure_reason=(
                f"vector_store_unavailable after 4 attempts: point of {subject_id} <{address}>"
            ),
            deleted_data_summary=None,
        ),
        _row(
            user_id=subject_id,
            failure_reason=f'duplicate key: Key (user_id)=({subject_id}) "{address}"',
            deleted_data_summary=None,
        ),
    ]
    response = admin.get(URL)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["failure_kind"] for item in items] == ["vector_store_unavailable", "other"]
    assert address not in response.text
    assert response.text.count(subject_id) == 2  # the ``user_id`` field of each item, no more
    assert "attempts" not in response.text.replace('"attempts"', "")
    assert "duplicate key" not in response.text


def test_the_statements_select_named_columns_of_one_table(admin, session) -> None:
    assert admin.get(URL).status_code == 200
    assert admin.get(f"{URL}/{session.rows[0].id}").status_code == 200
    count, page, one = session.statements
    for statement in (page, one):
        names = {column.name for column in statement.selected_columns}
        # S3: columns by name; the two derivation inputs are the only extras
        assert names == (ITEM_KEYS - {"request_id", "retry", "failure_kind"}) | {
            "id",
            "failure_reason",
            "deleted_data_summary",
        }
        assert not names & NEVER_SELECTED
    # the read by id asks for that id and nothing else
    where = _sql(one).split("WHERE")[1]
    assert where.split() == ["erasure_requests.id", "=", f"'{session.rows[0].id}'"]
    for statement in session.statements:
        for name in NEVER_SELECTED:
            assert name not in _sql(statement)


def test_the_list_is_ordered_and_paged_in_the_statement(admin, session) -> None:
    assert admin.get(f"{URL}?limit=7&offset=21").status_code == 200
    _, page = session.statements
    sql = _sql(page)
    assert sql.split("ORDER BY")[1].split("LIMIT")[0].split() == [
        "erasure_requests.requested_at",
        "DESC,",
        "erasure_requests.id",
        "ASC",
    ]
    assert "LIMIT 7" in sql and "OFFSET 21" in sql


def test_the_filters_are_exact_and_combine(admin, session) -> None:
    raw = "local:" + _token()
    response = admin.get(f"{URL}?status=complete&user_id={raw}&is_self_service=true")
    assert response.status_code == 200, response.text
    count, page = session.statements
    for statement in (count, page):
        where = _sql(statement).split("WHERE")[1].split("ORDER BY")[0]
        assert "erasure_requests.status = 'complete'" in where
        # D3 / D9: the stored value and nothing else (no pseudonym of it)
        assert f"erasure_requests.user_id = '{raw}'" in where
        assert "erasure_requests.is_self_service IS true" in where
        assert where.count(" AND ") == 2
        assert " OR " not in where and " IN " not in where and "LIKE" not in where
    session.statements.clear()
    assert admin.get(f"{URL}?is_self_service=false").status_code == 200
    for statement in session.statements:
        where = _sql(statement).split("WHERE")[1].split("ORDER BY")[0]
        assert "erasure_requests.is_self_service IS false" in where
        assert " AND " not in where
    session.statements.clear()
    assert admin.get(URL).status_code == 200
    for statement in session.statements:
        assert "WHERE" not in _sql(statement)


def test_total_is_the_count_of_the_filtered_rows(admin, session) -> None:
    session.total = 41
    body = admin.get(f"{URL}?limit=1").json()
    assert body["total"] == 41
    assert len(body["items"]) == 1


# --- log lines ---------------------------------------------------------------------


def test_the_list_log_line_names_the_caller_and_no_value(admin, session) -> None:
    raw = "local:" + _token()
    with capture_logs() as logs:
        response = admin.get(f"{URL}?user_id={raw}&status=failed&limit=5&offset=10")
    assert response.status_code == 200
    [entry] = [e for e in logs if e["event"] == "erasure_requests_listed"]
    assert entry == {
        "event": "erasure_requests_listed",
        "log_level": "info",
        "admin_user": "1001",
        "count": 1,
        "total": 1,
        "limit": 5,
        "offset": 10,
        "status": "failed",
        "is_self_service": None,
        "user_id_filter": True,
    }
    assert raw not in repr(logs)
    row = session.rows[0]
    for value in (row.user_id, row.failure_reason, *_unanswered_tokens(row)):
        assert value not in repr(logs)


def test_the_read_log_line_names_the_caller_and_the_request(admin, session) -> None:
    row = session.rows[0]
    with capture_logs() as logs:
        assert admin.get(f"{URL}/{row.id}").status_code == 200
    [entry] = [e for e in logs if e["event"] == "erasure_request_read"]
    assert entry == {
        "event": "erasure_request_read",
        "log_level": "info",
        "admin_user": "1001",
        "request_id": str(row.id),
    }
    for value in (row.user_id, row.failure_reason, *_unanswered_tokens(row)):
        assert value not in repr(logs)
