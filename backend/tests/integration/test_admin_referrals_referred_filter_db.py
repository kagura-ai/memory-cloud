"""``GET /api/v1/admin/referrals?referred_user_id=``: real rows over HTTP (#2836)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.referral import ReferralGrant
from utils.datetime import utcnow

from ._admin_helpers import make_user
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

LIST = "/api/v1/admin/referrals"
ROW_KEYS = {
    "id",
    "referrer_user_id",
    "referrer_workspace_id",
    "referred_user_id",
    "referred_workspace_id",
    "referrer_bonus_memories",
    "referred_bonus_memories",
    "granted_at",
    "revoked_at",
    "revoked_reason",
}


async def _user(db_session: AsyncSession) -> str:
    user = make_user(name="Referral User")
    db_session.add(user)
    await db_session.flush()
    return user.user_id


def _grant(referrer: str, referred: str, *, day: int = 1, revoked: bool = False) -> ReferralGrant:
    return ReferralGrant(
        id=uuid4(),
        referrer_user_id=referrer,
        referred_user_id=referred,
        referrer_bonus_memories=100,
        referred_bonus_memories=50,
        granted_at=datetime(2032, 1, day),
        revoked_at=utcnow() if revoked else None,
        revoked_reason="test" if revoked else None,
    )


async def test_referred_user_filter_returns_the_one_grant_or_none(
    db_session: AsyncSession,
) -> None:
    referrer = await _user(db_session)
    referred = await _user(db_session)
    other = await _user(db_session)
    nobody = await _user(db_session)
    grant = _grant(referrer, referred)
    db_session.add_all([grant, _grant(referrer, other, day=2)])
    await db_session.commit()

    async with admin_http(db_session) as client:
        hit = await client.get(LIST, params={"referred_user_id": referred})
        miss = await client.get(LIST, params={"referred_user_id": nobody})
        # Exact: a part of the id is not a match.
        partial = await client.get(LIST, params={"referred_user_id": referred[:-1]})

    assert hit.status_code == 200, hit.text
    body = hit.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 50, 0)
    (item,) = body["items"]
    assert set(item) == ROW_KEYS
    assert item["id"] == str(grant.id)
    assert item["referred_user_id"] == referred
    assert item["referrer_user_id"] == referrer
    assert miss.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert partial.json()["total"] == 0


async def test_a_revoked_grant_is_hidden_when_revoked_rows_are_excluded(
    db_session: AsyncSession,
) -> None:
    referrer = await _user(db_session)
    referred = await _user(db_session)
    grant = _grant(referrer, referred, revoked=True)
    db_session.add(grant)
    await db_session.commit()

    async with admin_http(db_session) as client:
        hidden = await client.get(
            LIST, params={"referred_user_id": referred, "include_revoked": "false"}
        )
        shown = await client.get(LIST, params={"referred_user_id": referred})

    assert hidden.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert [item["id"] for item in shown.json()["items"]] == [str(grant.id)]
    assert shown.json()["total"] == 1


async def test_referrer_and_referred_filters_must_both_match(db_session: AsyncSession) -> None:
    referrer = await _user(db_session)
    other_referrer = await _user(db_session)
    referred = await _user(db_session)
    other_referred = await _user(db_session)
    grant = _grant(referrer, referred)
    db_session.add_all([grant, _grant(other_referrer, other_referred, day=2)])
    await db_session.commit()

    async def ids(**params: str) -> tuple[list[str], int]:
        response = await client.get(LIST, params=params)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["items"]], response.json()["total"]

    async with admin_http(db_session) as client:
        assert await ids(referrer_user_id=referrer, referred_user_id=referred) == (
            [str(grant.id)],
            1,
        )
        assert await ids(referrer_user_id=other_referrer, referred_user_id=referred) == ([], 0)
        assert await ids(referrer_user_id=referrer, referred_user_id=other_referred) == ([], 0)
        # Paging applies to the filtered rows.
        assert await ids(referred_user_id=referred, offset="1") == ([], 1)


async def test_without_the_parameter_the_answer_is_unchanged(db_session: AsyncSession) -> None:
    referrer = await _user(db_session)
    first, second = await _user(db_session), await _user(db_session)
    grants = [_grant(referrer, first, day=3), _grant(referrer, second, day=4, revoked=True)]
    db_session.add_all(grants)
    await db_session.commit()

    async with admin_http(db_session) as client:
        by_referrer = await client.get(LIST, params={"referrer_user_id": referrer})
        active = await client.get(
            LIST, params={"referrer_user_id": referrer, "include_revoked": "false"}
        )
        everything = await client.get(LIST)

    # Newest first, both sides listed, revoked rows included by default.
    assert [item["id"] for item in by_referrer.json()["items"]] == [
        str(grants[1].id),
        str(grants[0].id),
    ]
    assert by_referrer.json()["total"] == 2
    assert [item["id"] for item in active.json()["items"]] == [str(grants[0].id)]
    assert set(everything.json()) == {"items", "total", "limit", "offset"}
    assert everything.json()["total"] >= 2
