"""The largest values the swept parameters take are answered by PostgreSQL (#2857).

Real rows over HTTP: a value one past a bound is refused by the parameter
(422, nothing stored), and the largest value the parameter takes (any text
without a NUL, the last integer of the column, the first and last day there
is) reaches the statement and is answered. The refusals themselves, with the
statements and log lines, are pinned in ``tests/api/test_param_bounds_sweep.py``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.main import app
from db.base import get_db
from models.auth import Workspace, WorkspaceMember
from models.referral import ReferralGrant
from models.worker_app import WorkerAppIdentity

from ._admin_helpers import make_context, make_user, make_workspace
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

ADMIN = "/api/v1/admin"
MAX_BIGINT = 2**63 - 1
# Every character a stored text may hold but a NUL: a line break, a percent
# sign (no wildcard), a space and a non-ASCII letter.
OTHER_TEXT = "a\nb% cé"
OTHER_PATH = "a%0Ab%25%20c%C3%A9"


@asynccontextmanager
async def owner_http(
    db_session: AsyncSession, owner_user_id: str
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """An HTTP client signed in as ``owner_user_id`` (the session middleware's principal)."""

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    principal = {
        "sub": owner_user_id,
        "user_id": owner_user_id,
        "email": f"{owner_user_id}@test.invalid",
        "role": "user",
    }

    async def _signed_in(scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["user"] = dict(principal)
        await app(scope, receive, send)

    app.dependency_overrides[get_db] = _db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_signed_in), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _owned_workspace(db_session: AsyncSession, plan_name: str = "pro"):
    owner, member = make_user(name="Owner"), make_user(name="Member")
    db_session.add_all([owner, member])
    await db_session.flush()
    workspace = make_workspace(owner_user_id=owner.user_id, plan_name=plan_name)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=workspace.id, user_id=owner.user_id, role="owner"),
            WorkspaceMember(workspace_id=workspace.id, user_id=member.user_id, role="member"),
        ]
    )
    context = make_context(workspace_id=workspace.id, created_by=owner.user_id)
    db_session.add(context)
    await db_session.commit()
    return owner.user_id, member.user_id, workspace.id, context.id


def _refused(response: httpx.Response) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"


# ---------------------------------------------------------------- admin routers


async def test_admin_user_reads_take_every_id_without_a_nul(db_session: AsyncSession) -> None:
    user = make_user(name="Read")
    db_session.add(user)
    await db_session.commit()
    user_id = user.user_id

    async with admin_http(db_session) as client:
        _refused(await client.get(f"{ADMIN}/users/{user_id}%00"))
        _refused(await client.get(f"{ADMIN}/users/{user_id}%00/stats"))
        _refused(
            await client.patch(
                f"{ADMIN}/users/{user_id}%00/workspace_slot_bonus", json={"delta": 1}
            )
        )
        missing = await client.get(f"{ADMIN}/users/{OTHER_PATH}")
        stats = await client.get(f"{ADMIN}/users/{user_id}/stats")
        detail = await client.get(f"{ADMIN}/users/{user_id}")

    assert missing.status_code == 404, missing.text
    assert stats.status_code == 200, stats.text
    assert detail.status_code == 200, detail.text


async def test_retry_failed_takes_every_uuid_spelling(db_session: AsyncSession) -> None:
    workspace_id = uuid4()
    async with admin_http(db_session) as client:
        _refused(await client.post(f"{ADMIN}/embedding/retry-failed?workspace_id=not-a-uuid"))
        for spelling in (str(workspace_id), str(workspace_id).upper(), workspace_id.hex):
            answered = await client.post(f"{ADMIN}/embedding/retry-failed?workspace_id={spelling}")
            assert answered.status_code == 200, (spelling, answered.text)
            assert answered.json() == {"status": "success", "reset_count": 0}


async def test_admin_plan_writes_take_a_typed_workspace_id(db_session: AsyncSession) -> None:
    owner, _, workspace_id, _ = await _owned_workspace(db_session, plan_name="pro")
    async with admin_http(db_session) as client:
        for path in ("plan", "quotas", "spend-cap"):
            _refused(await client.put(f"{ADMIN}/plans/workspaces/not-a-uuid/{path}", json={}))
        _refused(
            await client.put(
                f"{ADMIN}/plans/workspaces/{workspace_id}/plan",
                json={"plan_name": "basic", "reason": "a\u0000b"},
            )
        )
        plan = await client.put(
            f"{ADMIN}/plans/workspaces/{str(workspace_id).upper()}/plan",
            json={"plan_name": "basic", "reason": OTHER_TEXT},
        )
        quotas = await client.put(
            f"{ADMIN}/plans/workspaces/{workspace_id}/quotas", json={"addon_memory_bonus": 10000}
        )
        cap = await client.put(
            f"{ADMIN}/plans/workspaces/{workspace_id}/spend-cap",
            json={"embedding_daily_cap_usd": 1},
        )
        unknown = await client.put(
            f"{ADMIN}/plans/workspaces/{uuid4()}/plan", json={"plan_name": "pro"}
        )

    assert plan.status_code == 200, plan.text
    assert quotas.status_code == 200, quotas.text
    assert cap.status_code == 200, cap.text
    assert unknown.status_code == 404, unknown.text
    db_session.expire_all()
    stored = await db_session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    assert stored is not None and stored.plan_name == "basic"


async def test_force_transfer_refuses_a_nul_target_and_moves_nothing(
    db_session: AsyncSession,
) -> None:
    owner, member, workspace_id, _ = await _owned_workspace(db_session)
    async with admin_http(db_session) as client:
        _refused(
            await client.post(
                f"{ADMIN}/workspaces/{workspace_id}/force-transfer-ownership",
                json={"target_user_id": f"{member}\u0000", "reason": "the owner left"},
            )
        )
    db_session.expire_all()
    stored = await db_session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    assert stored is not None and stored.owner_user_id == owner


async def test_neural_config_takes_every_key_without_a_nul(db_session: AsyncSession) -> None:
    async with admin_http(db_session) as client:
        _refused(await client.get(f"{ADMIN}/neural-config?category=a%00b"))
        _refused(await client.get(f"{ADMIN}/neural-config/a%00b"))
        listed = await client.get(f"{ADMIN}/neural-config?category={OTHER_PATH}")
        missing = await client.get(f"{ADMIN}/neural-config/{OTHER_PATH}")
        put = await client.put(f"{ADMIN}/neural-config/{OTHER_PATH}", json={"value": OTHER_TEXT})

    assert listed.status_code == 200, listed.text
    assert listed.json()["configs"] == []
    assert missing.status_code == 404, missing.text
    assert put.status_code == 404, put.text


async def test_bm25_drift_takes_the_last_64_bit_offset_and_id(db_session: AsyncSession) -> None:
    async with admin_http(db_session) as client:
        _refused(await client.get(f"{ADMIN}/bm25-drift?offset={MAX_BIGINT + 1}"))
        _refused(await client.get(f"{ADMIN}/bm25-drift/{MAX_BIGINT + 1}"))
        page = await client.get(f"{ADMIN}/bm25-drift?offset={MAX_BIGINT}")
        row = await client.get(f"{ADMIN}/bm25-drift/{MAX_BIGINT}")

    assert page.status_code == 200, page.text
    assert page.json()["rows"] == []
    assert row.status_code == 404, row.text


async def test_undo_merge_takes_the_last_64_bit_action_id(db_session: AsyncSession) -> None:
    admin = make_user(name="Admin", role="admin")
    db_session.add(admin)
    await db_session.commit()
    principal = {"user_id": admin.user_id, "email": admin.email, "role": "admin"}
    async with admin_http(db_session, principal) as client:
        _refused(await client.post(f"{ADMIN}/sleep/actions/{MAX_BIGINT + 1}/undo-merge"))
        last = await client.post(f"{ADMIN}/sleep/actions/{MAX_BIGINT}/undo-merge")

    assert last.status_code == 404, last.text


async def test_promotion_takes_every_id_without_a_nul(db_session: AsyncSession) -> None:
    async with admin_http(db_session) as client:
        _refused(await client.post(f"{ADMIN}/system-admins", json={"user_id": "a\u0000b"}))
        missing = await client.post(f"{ADMIN}/system-admins", json={"user_id": OTHER_TEXT})

    assert missing.status_code == 404, missing.text


async def test_referral_revoke_refuses_a_nul_reason_and_keeps_the_grant(
    db_session: AsyncSession,
) -> None:
    referrer, referred = make_user(name="Referrer"), make_user(name="Referred")
    db_session.add_all([referrer, referred])
    await db_session.flush()
    grant = ReferralGrant(
        id=uuid4(),
        referrer_user_id=referrer.user_id,
        referred_user_id=referred.user_id,
        referrer_bonus_memories=100,
        referred_bonus_memories=50,
        granted_at=datetime(2033, 1, 1),
    )
    db_session.add(grant)
    await db_session.commit()
    grant_id = grant.id

    async with admin_http(db_session) as client:
        _refused(
            await client.post(f"{ADMIN}/referrals/{grant_id}/revoke", json={"reason": "a\u0000b"})
        )
        db_session.expire_all()
        kept = await db_session.scalar(select(ReferralGrant).where(ReferralGrant.id == grant_id))
        assert kept is not None and kept.revoked_at is None
        revoked = await client.post(
            f"{ADMIN}/referrals/{grant_id}/revoke", json={"reason": OTHER_TEXT}
        )

    assert revoked.status_code == 200, revoked.text
    db_session.expire_all()
    stored = await db_session.scalar(select(ReferralGrant).where(ReferralGrant.id == grant_id))
    assert stored is not None and stored.revoked_reason == OTHER_TEXT


async def test_worker_app_names_take_every_text_without_a_nul(db_session: AsyncSession) -> None:
    app_key = f"app-{uuid4().hex[:8]}"
    body = {"platform": "slack", "app_key": app_key, "signing_secret": "0" * 32}
    async with admin_http(db_session) as client:
        _refused(
            await client.post(f"{ADMIN}/worker-apps", json={**body, "display_name": "a\u0000"})
        )
        created = await client.post(
            f"{ADMIN}/worker-apps", json={**body, "display_name": OTHER_TEXT}
        )
        _refused(
            await client.patch(
                f"{ADMIN}/worker-apps/slack/{app_key}", json={"display_name": "\u0000"}
            )
        )

    assert created.status_code == 201, created.text
    stored = await db_session.scalar(
        select(WorkerAppIdentity).where(WorkerAppIdentity.app_key == app_key)
    )
    assert stored is not None and stored.display_name == OTHER_TEXT


# ------------------------------------------------------------ workspace routers


async def test_workspace_texts_and_member_ids_take_everything_but_a_nul(
    db_session: AsyncSession,
) -> None:
    owner, member, workspace_id, _ = await _owned_workspace(db_session)
    base = f"/api/v1/workspaces/{workspace_id}"
    async with owner_http(db_session, owner) as client:
        _refused(await client.put(base, json={"description": "a\u0000b"}))
        _refused(await client.put(f"{base}/members/{member}%00", json={"role": "viewer"}))
        _refused(await client.get(f"{base}/members/{member}%00/context-access"))
        _refused(await client.post(f"{base}/transfer-ownership", json={"target_user_id": "\u0000"}))
        updated = await client.put(base, json={"name": "Renamed", "description": OTHER_TEXT})
        missing = await client.get(f"{base}/members/{OTHER_PATH}/context-access")
        access = await client.get(f"{base}/members/{member}/context-access")
        added = await client.post(f"{base}/members", json={"user_id": OTHER_TEXT, "role": "member"})

    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == OTHER_TEXT
    assert missing.status_code == 404, missing.text
    assert access.status_code == 200, access.text
    # A member row may name an account id that holds any other character.
    assert added.status_code == 201, added.text
    assert added.json()["user_id"] == OTHER_TEXT


async def test_a_timeline_takes_every_window_whose_first_day_is_a_date(
    db_session: AsyncSession,
) -> None:
    owner, _, workspace_id, context_id = await _owned_workspace(db_session)
    base = f"/api/v1/workspaces/{workspace_id}"
    # The window starts ``days - 1`` days back; the last day there is ends it.
    first = 1 - (date.max - datetime.now(UTC).date()).days + 1
    async with owner_http(db_session, owner) as client:
        for path in (
            "memory-timeline",
            f"contexts/{context_id}/usage-timeline",
            f"contexts/{context_id}/user-activity",
        ):
            _refused(await client.get(f"{base}/{path}?days={first - 2}"))
            answered = await client.get(f"{base}/{path}?days={first}")
            assert answered.status_code == 200, (path, answered.text)
            clamped = await client.get(f"{base}/{path}?days={MAX_BIGINT + 1}")
            assert clamped.status_code == 200, (path, clamped.text)


async def test_member_credentials_take_every_id_and_delay_the_calendar_holds(
    db_session: AsyncSession,
) -> None:
    owner, member, workspace_id, _ = await _owned_workspace(db_session)
    base = f"/api/v1/workspaces/{workspace_id}/members"
    # The last delay whose hide time is a date, with a day to spare.
    last = int((datetime.max.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() // 60)
    async with owner_http(db_session, owner) as client:
        _refused(await client.get(f"{base}/{member}%00/credentials"))
        _refused(
            await client.post(
                f"{base}/{owner}/credentials/api-keys",
                json={"name": "k", "auto_hide_minutes": last + 2 * 24 * 60},
            )
        )
        _refused(await client.post(f"{base}/{owner}/credentials/api-keys", json={"name": "\u0000"}))
        listed = await client.get(f"{base}/{member}/credentials")
        missing = await client.get(f"{base}/{OTHER_PATH}/credentials")
        far = await client.post(
            f"{base}/{owner}/credentials/api-keys",
            json={"name": "far", "auto_hide_minutes": last - 24 * 60},
        )
        past = await client.post(
            f"{base}/{owner}/credentials/api-keys",
            json={"name": "past", "auto_hide_minutes": -5},
        )

    assert listed.status_code == 200, listed.text
    assert missing.status_code in (403, 404), missing.text
    assert far.status_code == 201, far.text
    assert past.status_code == 201, past.text


async def test_operation_history_takes_every_text_and_time_utc_holds(
    db_session: AsyncSession,
) -> None:
    owner, _, workspace_id, _ = await _owned_workspace(db_session, plan_name="pro")
    url = f"/api/v1/workspaces/{workspace_id}/operation-history"
    async with owner_http(db_session, owner) as client:
        _refused(await client.get(f"{url}?query=a%00b"))
        _refused(await client.get(f"{url}?actor=a%00b"))
        _refused(await client.get(f"{url}?since=0001-01-01T00:00:00%2B05:00"))
        _refused(await client.get(f"{url}?until=9999-12-31T23:59:59-05:00"))
        answered = await client.get(
            url,
            params={
                "query": OTHER_TEXT,
                "actor": OTHER_TEXT,
                "since": "0001-01-01T00:00:00-05:00",
                "until": "9999-12-31T23:59:59+05:00",
            },
        )
        naive = await client.get(f"{url}?since=2020-01-01T00:00:00")

    assert answered.status_code == 200, answered.text
    assert answered.json()["items"] == []
    # A time without a zone keeps its own message.
    assert naive.status_code == 422
    assert naive.json()["message"] == "Time filters require a timezone"


async def test_workspace_plan_takes_a_typed_workspace_id(db_session: AsyncSession) -> None:
    owner, _, workspace_id, _ = await _owned_workspace(db_session)
    async with owner_http(db_session, owner) as client:
        _refused(await client.get("/api/v1/workspaces/not-a-uuid/plan"))
        answered = await client.get(f"/api/v1/workspaces/{str(workspace_id).upper()}/plan")

    assert answered.status_code == 200, answered.text
    assert answered.json()["workspace_id"] == str(workspace_id)


async def test_workspace_sleep_reports_take_the_last_offset_and_every_user_id(
    db_session: AsyncSession,
) -> None:
    owner, _, workspace_id, _ = await _owned_workspace(db_session)
    url = f"/api/v1/workspaces/{workspace_id}/sleep-reports"
    async with owner_http(db_session, owner) as client:
        _refused(await client.get(f"{url}?offset={MAX_BIGINT + 1}"))
        _refused(await client.get(f"{url}?user_id={owner}%00"))
        last = await client.get(f"{url}?offset={MAX_BIGINT}")
        other = await client.get(f"{url}?user_id={OTHER_PATH}")

    assert last.status_code == 200, last.text
    assert last.json()["reports"] == []
    assert other.status_code == 200, other.text
    assert other.json()["total"] == 0
