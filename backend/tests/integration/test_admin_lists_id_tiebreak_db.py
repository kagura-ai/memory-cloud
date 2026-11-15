"""Two admin lists end their order with ``id``, on real Postgres (#2858).

``GET /api/v1/admin/sleep-reports`` (``started_at`` newest first) and
``GET /api/v1/admin/referrals`` (``granted_at`` newest first) page with
``offset``. Rows that share the sort time are ordered by ``id``, so:

- walking every page returns every row exactly once;
- rows with the same time come in ``id`` order, on every read;
- rows with different times keep their newest-first order.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.referral import ReferralGrant
from models.sleep import SleepReport
from services.sleep_reporter_service import SleepReporterService

from ._admin_helpers import make_user
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

T0 = datetime(2033, 3, 1, 12, 0, 0)
# Seven rows share the newest time, three an older one, one is oldest.
TIMES = [T0] * 7 + [T0 - timedelta(hours=1)] * 3 + [T0 - timedelta(hours=2)]


def _expected(rows: list[tuple[datetime, UUID]]) -> list[str]:
    """Newest first, then ``id`` ascending (PostgreSQL's uuid order)."""
    by_id = sorted(rows, key=lambda row: str(row[1]))
    return [str(row_id) for _, row_id in sorted(by_id, key=lambda row: row[0], reverse=True)]


async def _walk(
    client: httpx.AsyncClient, url: str, key: str, params: dict[str, str], limit: int
) -> list[str]:
    """Every page of ``url`` at ``limit`` rows, as one list of ids."""
    ids: list[str] = []
    offset = 0
    while True:
        response = await client.get(url, params=params | {"limit": limit, "offset": offset})
        assert response.status_code == 200, response.text
        page = [item["id"] for item in response.json()[key]]
        ids.extend(page)
        offset += limit
        if offset >= response.json()["total"]:
            return ids


async def test_sleep_report_pages_return_each_row_once_with_ties_in_id_order(
    db_session: AsyncSession,
) -> None:
    user_id = f"tiebreak-sleeper-{uuid4().hex[:8]}"
    reports = [
        SleepReport(id=uuid4(), user_id=user_id, started_at=at, status="completed") for at in TIMES
    ]
    db_session.add_all(reports)
    await db_session.commit()
    expected = _expected([(r.started_at, r.id) for r in reports])

    async with admin_http(db_session) as client:
        for limit in (1, 2, 3, 4):
            walked = await _walk(
                client, "/api/v1/admin/sleep-reports", "reports", {"user_id": user_id}, limit
            )
            assert walked == expected, f"limit={limit}"
        whole = await _walk(
            client, "/api/v1/admin/sleep-reports", "reports", {"user_id": user_id}, 50
        )

    assert whole == expected
    assert len(set(whole)) == len(reports)


async def test_workspace_sleep_report_pages_share_the_order(db_session: AsyncSession) -> None:
    """``GET /api/v1/workspaces/{id}/sleep-reports`` reads the same query, scoped by workspace."""
    workspace_id = uuid4()
    reports = [
        SleepReport(
            id=uuid4(),
            user_id=f"tiebreak-ws-{uuid4().hex[:8]}",
            workspace_id=workspace_id,
            started_at=at,
            status="completed",
        )
        for at in TIMES
    ]
    # Another workspace's report at the same time never shows up.
    db_session.add_all(
        [*reports, SleepReport(user_id="other", workspace_id=uuid4(), started_at=T0)]
    )
    await db_session.commit()
    expected = _expected([(r.started_at, r.id) for r in reports])

    service = SleepReporterService(db_session)
    for limit in (1, 2, 3, 4):
        walked: list[str] = []
        for offset in range(0, len(reports), limit):
            page, total = await service.list_reports(
                workspace_id=workspace_id, limit=limit, offset=offset
            )
            assert total == len(reports)
            walked.extend(str(report.id) for report in page)
        assert walked == expected, f"limit={limit}"


async def test_referral_pages_return_each_row_once_with_ties_in_id_order(
    db_session: AsyncSession,
) -> None:
    referrer = make_user(name="Tiebreak Referrer")
    referred = [make_user(name=f"Tiebreak Referred {i}") for i in range(len(TIMES))]
    db_session.add_all([referrer, *referred])
    await db_session.flush()
    grants = [
        ReferralGrant(
            id=uuid4(),
            referrer_user_id=referrer.user_id,
            referred_user_id=user.user_id,
            referrer_bonus_memories=100,
            referred_bonus_memories=50,
            granted_at=at,
        )
        for user, at in zip(referred, TIMES, strict=True)
    ]
    db_session.add_all(grants)
    await db_session.commit()
    expected = _expected([(g.granted_at, g.id) for g in grants])
    params = {"referrer_user_id": referrer.user_id}

    async with admin_http(db_session) as client:
        for limit in (1, 2, 3, 4):
            walked = await _walk(client, "/api/v1/admin/referrals", "items", params, limit)
            assert walked == expected, f"limit={limit}"
        whole = await _walk(client, "/api/v1/admin/referrals", "items", params, 50)

    assert whole == expected
    assert len(set(whole)) == len(grants)
