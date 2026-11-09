"""#2748: the weekly usage numbers count 7 calendar days, today included.

``GET /workspace/usage/current`` (``*_calls_this_week``, ``weekly_api_usage``
and the ``*_week`` quota meters) and ``GET /workspace/usage/members``
(``api_calls_week``) used ``date >= today - 7``: 8 calendar days under a
"this week" label. They now count today and the 6 days before it, the same
window as the context-stats KPI (#2744).

Seeded rows, workspace W with owner ``o`` and member ``m``:

  * ``o`` — today ``mcp:recall`` and today ``/api/v1/memory/recall`` (REST);
            6 days ago ``mcp:remember`` (inside); 7 days ago ``mcp:remember``
            (outside).
  * ``m`` — today ``mcp:recall``; 6 days ago a public REST call (inside);
            7 days ago ``mcp:recall`` (outside).
  * ``o`` — today ``mcp:recall`` in another workspace (never counted).

The handlers read ``utcnow()`` once per request; the test freezes it at the
instant the rows are dated from, so a run across midnight UTC stays exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio

from api.routes import workspace as workspace_routes
from api.routes.workspace import get_workspace_member_usage, get_workspace_usage_current
from models.auth import UsageStats, WorkspaceMember, WorkspaceRole
from utils.datetime import utcnow

from ._admin_helpers import make_user, make_workspace

MCP = "mcp:recall"
REST = "/api/v1/memory/recall"
PUBLIC = "/api/v1/public/contexts/recall"


def _usage(user_id: str, workspace_id: UUID, endpoint: str, day: date) -> UsageStats:
    return UsageStats(
        user_id=user_id,
        endpoint=endpoint,
        method="POST",
        status_code=200,
        response_time_ms=10,
        created_at=utcnow(),
        date=day,
        workspace_id=workspace_id,
    )


@dataclass
class Seeded:
    now: datetime
    ws: UUID
    owner: str
    member: str


@pytest_asyncio.fixture
async def seeded(db_session, monkeypatch) -> Seeded:
    now = utcnow()
    monkeypatch.setattr(workspace_routes, "utcnow", lambda: now)
    today = now.date()
    inside, outside = today - timedelta(days=6), today - timedelta(days=7)

    owner, member = make_user(), make_user()
    db_session.add_all([owner, member])
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id)
    other = make_workspace(owner_user_id=owner.user_id)
    db_session.add_all([ws, other])
    await db_session.flush()
    o, m = owner.user_id, member.user_id
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=o, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws.id, user_id=m, role=WorkspaceRole.MEMBER),
            WorkspaceMember(workspace_id=other.id, user_id=o, role=WorkspaceRole.OWNER),
            _usage(o, ws.id, MCP, today),
            _usage(o, ws.id, REST, today),
            _usage(o, ws.id, "mcp:remember", inside),
            _usage(o, ws.id, "mcp:remember", outside),
            _usage(m, ws.id, MCP, today),
            _usage(m, ws.id, PUBLIC, inside),
            _usage(m, ws.id, MCP, outside),
            _usage(o, other.id, MCP, today),
        ]
    )
    await db_session.commit()
    return Seeded(now=now, ws=ws.id, owner=o, member=m)


@pytest.mark.asyncio
async def test_usage_current_counts_today_and_the_six_days_before(seeded, db_session) -> None:
    user = {"user_id": seeded.owner, "current_workspace_id": seeded.ws}

    response = await get_workspace_usage_current(user=user, db=db_session)

    usage = response.usage
    # Today's numbers are unchanged by the window.
    assert usage.api_calls_today == 3
    assert usage.mcp_calls_today == 2
    assert usage.rest_calls_today == 1
    assert usage.public_calls_today == 0
    # The two rows dated 7 days ago are outside; the two dated 6 days ago are inside.
    assert usage.api_calls_this_week == 5
    assert usage.mcp_calls_this_week == 3
    assert usage.rest_calls_this_week == 1
    assert usage.public_calls_this_week == 1
    assert response.weekly_api_usage.current == 5
    assert response.daily_api_usage.current == 3
    meters = {m.id: m for m in response.quota_usage or []}
    assert meters["mcp_week"].used == 3
    assert meters["public_week"].used == 1
    assert meters["mcp_day"].used == 2


@pytest.mark.asyncio
async def test_member_usage_counts_today_and_the_six_days_before(seeded, db_session) -> None:
    user = {"user_id": seeded.owner, "current_workspace_id": seeded.ws}

    response = await get_workspace_member_usage(user=user, db=db_session)

    rows = {entry.user_id: entry for entry in response.members}
    assert set(rows) == {seeded.owner, seeded.member}
    assert rows[seeded.owner].api_calls_today == 2
    assert rows[seeded.owner].api_calls_week == 3  # not the mcp:remember of 7 days ago
    assert rows[seeded.member].api_calls_today == 1
    assert rows[seeded.member].api_calls_week == 2  # not the mcp:recall of 7 days ago
