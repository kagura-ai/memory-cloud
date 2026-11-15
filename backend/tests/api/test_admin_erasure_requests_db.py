"""Admin reads of erasure requests against Postgres (#2834).

``api.main.app`` with ``get_db`` on the test database and real
``erasure_requests`` rows. Every column the route does not answer is seeded
with a unique token, and each test reads the raw response body for it.

The rows are dated far in the future so they lead the list whatever other
tests left behind, and are removed again afterwards.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog
from models.erasure import (
    INTERRUPTED_AFTER_POSTGRES_COMMIT,
    VALID_STATUSES,
    ErasureRequest,
)
from services import account_erasure_service
from services.erasure_details import erasure_pseudonym

pytestmark = pytest.mark.asyncio(loop_scope="session")

URL = "/api/v1/admin/erasure-requests"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
OPERATOR = "ops@example.com"
FAR = datetime(2096, 1, 1, tzinfo=UTC)

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
UNANSWERED = (
    "user_email_hash",
    "reason_detail",
    "confirm_token_hash",
    "ip_address",
    "user_agent",
    "failure_reason",
)


def _token() -> str:
    return "tok" + secrets.token_hex(12)


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    ids: list[uuid.UUID] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def token(self) -> str:
        value = _token()
        self.tokens.append(value)
        return value

    async def seed(self, status: str, *, minutes: int = 0, **overrides: Any) -> dict[str, Any]:
        """Insert one request; every unanswered column holds its own token."""
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "user_id": "local:" + _token(),
            "user_email_hash": self.token(),
            "initiated_by": "admin-" + _token(),
            "is_self_service": False,
            "reason_code": "user_request_via_support",
            "reason_detail": self.token(),
            "status": status,
            "confirm_token_hash": self.token(),
            "requested_at": FAR + timedelta(minutes=minutes),
            "failure_reason": self.token(),
            "deleted_data_summary": None,
            "ip_address": self.token(),
            "user_agent": self.token(),
        }
        values.update(overrides)
        async with self.factory() as db:
            await db.execute(insert(ErasureRequest).values(**values))
            await db.commit()
        self.ids.append(values["id"])
        return values

    async def get(self, query: str = "", **kw: Any) -> httpx.Response:
        return await self.client.get(URL + query, **kw)

    def assert_no_token(self, response: httpx.Response) -> None:
        assert self.tokens
        for token in self.tokens:
            assert token not in response.text
        for name in (*UNANSWERED, "deleted_data_summary"):
            assert name not in response.text


@pytest.fixture
async def world(async_engine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            yield db

    async def _admin() -> dict:
        return dict(ADMIN_SESSION)

    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
    monkeypatch.setattr(account_erasure_service, "_audit_salt", lambda: "salt-" + "s" * 32)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_optional] = _admin
    transport = httpx.ASGITransport(app=app)
    state: World | None = None
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            state = World(factory=maker, client=client)
            yield state
    finally:
        app.dependency_overrides.clear()
        if state is not None and state.ids:
            async with maker() as db:
                await db.execute(delete(ErasureRequest).where(ErasureRequest.id.in_(state.ids)))
                await db.commit()


async def _count(world: World, *conditions: Any) -> int:
    async with world.factory() as db:
        return (
            await db.scalar(select(func.count()).select_from(ErasureRequest).where(*conditions))
        ) or 0


async def test_each_status_is_listed_and_no_unanswered_column_is_answered(world: World) -> None:
    # N1: a token in every unanswered column of a row in each of the six statuses
    order = ["pending", "cooling_off", "in_progress", "complete", "failed", "cancelled"]
    assert set(order) == VALID_STATUSES
    subject_id, address = "local:" + world.token(), world.token() + "@example.com"
    seeded = {}
    for minutes, status in enumerate(order):
        overrides: dict[str, Any] = {}
        if status == "failed":
            overrides["failure_reason"] = (
                "vector_store_unavailable after 4 attempts: "
                f"could not delete the points of {subject_id} <{address}>"
            )
        seeded[status] = await world.seed(status, minutes=minutes, **overrides)

    listed = await world.get("?limit=6")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == await _count(world)
    assert (body["limit"], body["offset"]) == (6, 0)
    # newest request first
    assert [item["status"] for item in body["items"]] == order[::-1]
    world.assert_no_token(listed)

    for item in body["items"]:
        row = seeded[item["status"]]
        # N5: exactly the named fields
        assert set(item) == ITEM_KEYS
        assert item == {
            "request_id": str(row["id"]),
            "user_id": row["user_id"],
            "status": row["status"],
            "is_self_service": False,
            "reason_code": "user_request_via_support",
            "initiated_by": row["initiated_by"],
            "requested_at": row["requested_at"].isoformat().replace("+00:00", "Z"),
            "confirmed_at": None,
            "scheduled_for": None,
            "started_at": None,
            "completed_at": None,
            "cancelled_at": None,
            "retry": None,
            # N3: a kind for the failed request only
            "failure_kind": "vector_store_unavailable" if row["status"] == "failed" else None,
        }
        one = await world.get(f"/{row['id']}")
        assert one.status_code == 200, one.text
        assert one.json() == item
        world.assert_no_token(one)


async def test_the_summary_is_answered_as_the_named_retry_keys_only(world: World) -> None:
    # N2: tokens across the summary, inside the retry block and in a retry value
    summary = {
        "workspaces": {
            "transferred": [{"workspace_id": world.token(), "new_owner_user_id": world.token()}]
        },
        "postgres": {"memories": 3, "marker": world.token()},
        "note": world.token(),
        "pseudonym_salt": world.token(),
        "retry": {
            "attempts": 4,
            "step": "vector_store",
            "error_class": world.token() + " with spaces",
            "last_attempt_at": "2026-11-14T12:00:00Z",
            "embeddings_requeued": 17,
            "requeued_at": "2026-11-14T12:00:01Z",
            "extra": world.token(),
        },
    }
    row = await world.seed(
        "failed",
        failure_reason=f"lock_conflict after 4 attempts: postgres: {world.token()}",
        deleted_data_summary=summary,
    )
    for response in (await world.get("?limit=1"), await world.get(f"/{row['id']}")):
        assert response.status_code == 200, response.text
        world.assert_no_token(response)
        body = response.json()
        item = body["items"][0] if "items" in body else body
        assert item["request_id"] == str(row["id"])
        assert item["failure_kind"] == "lock_conflict"
        assert set(item["retry"]) == RETRY_KEYS
        assert item["retry"] == {
            "attempts": 4,
            "step": "vector_store",
            "error_class": None,
            "last_attempt_at": "2026-11-14T12:00:00Z",
            "embeddings_requeued": 17,
        }
        for key in ("workspaces", "postgres", "pseudonym_salt", "requeued_at", "extra"):
            assert key not in response.text


async def test_failure_kind_of_stored_rows(world: World) -> None:
    # N3: no known prefix, and no reason at all, answer ``other``
    free_text = await world.seed(
        "failed", minutes=3, failure_reason=f"duplicate key value: Key (user_id)=({world.token()})"
    )
    no_reason = await world.seed("failed", minutes=2, failure_reason=None)
    interrupted = await world.seed(
        "failed", minutes=1, failure_reason=INTERRUPTED_AFTER_POSTGRES_COMMIT
    )
    cancelled = await world.seed(
        "cancelled", minutes=0, failure_reason="vector_store_unavailable after 1 attempts: x"
    )
    response = await world.get("?limit=4")
    assert response.status_code == 200, response.text
    assert [(item["request_id"], item["failure_kind"]) for item in response.json()["items"]] == [
        (str(free_text["id"]), "other"),
        (str(no_reason["id"]), "other"),
        (str(interrupted["id"]), "after_postgres_commit"),
        (str(cancelled["id"]), None),
    ]
    world.assert_no_token(response)
    assert "duplicate key" not in response.text


async def test_a_complete_request_is_found_by_its_pseudonym_only(world: World) -> None:
    raw = "local:" + _token()
    pseudonym = erasure_pseudonym(raw)
    admin_id = "admin-" + _token()
    row = await world.seed(
        "complete",
        user_id=pseudonym,
        initiated_by=admin_id,
        completed_at=FAR + timedelta(hours=1),
        failure_reason=None,
        deleted_data_summary={"postgres": {"memories": 1}, "pseudonym_salt": world.token()},
    )
    # N4: the stored value is what the filter compares
    by_raw = await world.get(f"?user_id={raw}")
    assert by_raw.status_code == 200, by_raw.text
    assert by_raw.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    by_pseudonym = await world.get(f"?user_id={pseudonym}")
    assert by_pseudonym.status_code == 200, by_pseudonym.text
    body = by_pseudonym.json()
    assert body["total"] == 1
    [item] = body["items"]
    assert item["request_id"] == str(row["id"])
    # the pseudonym as ``user_id``, the admin's id as ``initiated_by``
    assert (item["user_id"], item["initiated_by"]) == (pseudonym, admin_id)
    assert item["status"] == "complete"
    assert item["completed_at"] == "2096-01-01T01:00:00Z"
    assert (item["retry"], item["failure_kind"]) == (None, None)
    assert raw not in by_pseudonym.text
    world.assert_no_token(by_pseudonym)


async def test_a_rescheduled_request_shows_its_schedule_and_retry_block(world: World) -> None:
    # What ERASURE-007 leaves: cooling_off, the next run, the service's retry block
    block = account_erasure_service._retry_block(1, "ResponseHandlingException")
    scheduled = FAR + timedelta(minutes=30)
    row = await world.seed(
        "cooling_off",
        scheduled_for=scheduled,
        started_at=None,
        failure_reason=None,
        deleted_data_summary={"retry": block},
    )
    response = await world.get(f"/{row['id']}")
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["status"] == "cooling_off"
    assert item["scheduled_for"] == "2096-01-01T00:30:00Z"
    assert item["failure_kind"] is None
    assert item["retry"] == {
        "attempts": 1,
        "step": "vector_store",
        "error_class": "ResponseHandlingException",
        "last_attempt_at": block["last_attempt_at"],
        "embeddings_requeued": None,
    }
    assert block["last_attempt_at"].endswith("Z")
    world.assert_no_token(response)


async def test_the_filters_work_alone_and_together_and_total_counts_the_matches(
    world: World,
) -> None:
    shared = "local:" + _token()
    failed = await world.seed("failed", minutes=5, user_id=shared)
    cancelled_self = await world.seed(
        "cancelled", minutes=4, user_id=shared, is_self_service=True, reason_code="self_service"
    )
    cancelled_admin = await world.seed("cancelled", minutes=3, user_id=shared)
    other = await world.seed(
        "cancelled", minutes=2, is_self_service=True, reason_code="self_service"
    )

    def ids(response: httpx.Response) -> list[str]:
        return [item["request_id"] for item in response.json()["items"]]

    by_user = await world.get(f"?user_id={shared}")
    assert by_user.json()["total"] == 3
    assert ids(by_user) == [str(r["id"]) for r in (failed, cancelled_self, cancelled_admin)]

    by_status = await world.get("?status=cancelled&limit=3")
    assert by_status.json()["total"] == await _count(world, ErasureRequest.status == "cancelled")
    assert ids(by_status) == [str(r["id"]) for r in (cancelled_self, cancelled_admin, other)]

    by_kind = await world.get("?is_self_service=true&limit=2")
    assert by_kind.json()["total"] == await _count(world, ErasureRequest.is_self_service.is_(True))
    assert ids(by_kind) == [str(r["id"]) for r in (cancelled_self, other)]
    not_self = await world.get("?is_self_service=false&limit=2")
    assert ids(not_self) == [str(r["id"]) for r in (failed, cancelled_admin)]

    two = await world.get(f"?user_id={shared}&status=cancelled")
    assert (two.json()["total"], ids(two)) == (
        2,
        [str(r["id"]) for r in (cancelled_self, cancelled_admin)],
    )
    three = await world.get(f"?user_id={shared}&status=cancelled&is_self_service=true")
    assert (three.json()["total"], ids(three)) == (1, [str(cancelled_self["id"])])
    none = await world.get(f"?user_id={shared}&status=complete")
    assert none.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    for response in (by_user, by_status, by_kind, not_self, two, three):
        world.assert_no_token(response)


async def test_the_order_is_newest_first_then_id_and_pages_do_not_overlap(world: World) -> None:
    shared = "local:" + _token()
    same_time = sorted(uuid.uuid4() for _ in range(3))
    # inserted against the id order, all at one instant, plus one older row
    for request_id in reversed(same_time):
        await world.seed("failed", minutes=9, user_id=shared, id=request_id)
    older = await world.seed("cancelled", minutes=1, user_id=shared)
    expected = [str(request_id) for request_id in same_time] + [str(older["id"])]

    seen: list[str] = []
    for offset in (0, 2, 4):
        page = await world.get(f"?user_id={shared}&limit=2&offset={offset}")
        assert page.status_code == 200, page.text
        body = page.json()
        assert (body["total"], body["limit"], body["offset"]) == (4, 2, offset)
        seen.extend(item["request_id"] for item in body["items"])
    assert seen == expected


async def test_an_unknown_id_is_404_and_a_bad_one_422(world: World) -> None:
    await world.seed("pending")
    unknown = await world.get(f"/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "RES-001"
    assert (await world.get("/not-a-uuid")).status_code == 422
    assert (await world.get("?status=unknown")).status_code == 422


async def test_an_operator_reads_in_service_mode(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await world.seed("in_progress", initiated_by=f"operator:{OPERATOR}")
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    app.dependency_overrides.pop(get_current_user_optional, None)
    headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}

    one = await world.get(f"/{row['id']}", headers=headers)
    assert one.status_code == 200, one.text
    assert one.json()["initiated_by"] == f"operator:{OPERATOR}"
    listed = await world.get(f"?user_id={row['user_id']}", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert (await world.get(f"/{row['id']}")).status_code == 401
    monkeypatch.setattr(settings, "system_admin_mode", "off")
    assert (await world.get(f"/{row['id']}", headers=headers)).status_code == 404


async def test_the_reads_change_no_row_and_write_no_audit_row(world: World) -> None:
    row = await world.seed("failed", deleted_data_summary={"retry": {"attempts": 0}})

    async def snapshot() -> tuple[Any, int]:
        async with world.factory() as db:
            stored = (
                await db.execute(
                    select(ErasureRequest.__table__).where(ErasureRequest.id == row["id"])
                )
            ).one()
            audit_rows = await db.scalar(select(func.count()).select_from(AuditLog))
            return tuple(stored), audit_rows or 0

    before = await snapshot()
    assert (await world.get("?limit=5")).status_code == 200
    assert (await world.get(f"/{row['id']}")).status_code == 200
    assert await snapshot() == before
