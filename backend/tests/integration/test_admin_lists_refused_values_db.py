"""Values the database would refuse answer 422 on the admin lists (#2841, #2831, #2830, #2836).

Real rows over HTTP: a NUL in an exact-match filter and an ``offset`` past a
64-bit integer are refused by the parameter, and the largest values the
parameters take are answered by the database.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.referral import ReferralGrant

from ._admin_helpers import make_user, make_workspace
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

WORKSPACES = "/api/v1/admin/plans/workspaces"
AUDIT = "/api/v1/admin/plans/audit"
ALLOWLIST = "/api/v1/admin/signup-gate/allowlist"
REFERRALS = "/api/v1/admin/referrals"

MAX_OFFSET = 2**63 - 1


@pytest.mark.parametrize("url", [WORKSPACES, AUDIT, ALLOWLIST, REFERRALS])
async def test_an_offset_past_64_bits_is_422_and_the_largest_is_answered(
    db_session: AsyncSession, url: str
) -> None:
    async with admin_http(db_session) as client:
        refused = await client.get(f"{url}?offset={MAX_OFFSET + 1}")
        far = await client.get(f"{url}?offset=99999999999999999999")
        last = await client.get(f"{url}?offset={MAX_OFFSET}")

    for response in (refused, far):
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "VAL-001"
    assert last.status_code == 200, last.text
    body = last.json()
    assert (body if url == AUDIT else body["items"]) == []


async def test_a_nul_in_the_owner_filter_is_422(db_session: AsyncSession) -> None:
    owner = make_user(name="Refused Owner")
    db_session.add(owner)
    await db_session.flush()
    db_session.add(make_workspace(owner_user_id=owner.user_id))
    await db_session.commit()
    owner_id = owner.user_id

    async with admin_http(db_session) as client:
        for query in (f"?owner_user_id={owner_id}%00", "?owner_user_id=%00"):
            refused = await client.get(f"{WORKSPACES}{query}")
            assert refused.status_code == 422, query
            assert refused.json()["error"] == "VAL-001"
            assert owner_id not in refused.text
        # Any other character is compared as it is: nothing is a wildcard.
        for value in ("%25", "_", f"{owner_id[:-1]}_", "a%0Ab%20c"):
            answered = await client.get(f"{WORKSPACES}?owner_user_id={value}")
            assert answered.status_code == 200, value
            assert answered.json()["total"] == 0, value
        found = await client.get(WORKSPACES, params={"owner_user_id": owner_id})

    assert found.json()["total"] == 1


@pytest.mark.parametrize("parameter", ["referred_user_id", "referrer_user_id"])
async def test_a_nul_in_a_referral_filter_is_422(db_session: AsyncSession, parameter: str) -> None:
    referrer, referred = make_user(name="Referrer"), make_user(name="Referred")
    db_session.add_all([referrer, referred])
    await db_session.flush()
    db_session.add(
        ReferralGrant(
            id=uuid4(),
            referrer_user_id=referrer.user_id,
            referred_user_id=referred.user_id,
            referrer_bonus_memories=100,
            referred_bonus_memories=50,
            granted_at=datetime(2033, 1, 1),
        )
    )
    await db_session.commit()
    user_id = referred.user_id if parameter == "referred_user_id" else referrer.user_id

    async with admin_http(db_session) as client:
        for query in (f"?{parameter}={user_id}%00", f"?{parameter}=%00"):
            refused = await client.get(f"{REFERRALS}{query}")
            assert refused.status_code == 422, query
            assert refused.json()["error"] == "VAL-001"
            assert user_id not in refused.text
        for value in ("%25", "_", f"{user_id[:-1]}_", "a%0Ab%20c"):
            answered = await client.get(f"{REFERRALS}?{parameter}={value}")
            assert answered.status_code == 200, value
            assert answered.json()["total"] == 0, value
        found = await client.get(REFERRALS, params={parameter: user_id})

    assert found.json()["total"] == 1
