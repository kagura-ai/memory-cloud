"""A destructive commit rechecks its human approver: a lost seat is a conflict (#2912).

The worker commits a destructive diff only while the Editor who approved it
still qualifies. When that approver lost a seat, the Editor role or the reach
to the context between the approval and the commit, the worker's commit
answers the fixed ``RES-002`` conflict ("Approval authority changed"): the
refusal is about the approver, never about the worker, so no
``sync.authority.denied`` row is written for it.
"""

from uuid import UUID

import pytest
from sqlalchemy import select

from api.routes import sync_feeds as feed_routes
from models.auth import ContextMember, WorkspaceMember
from models.sync_feed import SyncRun
from tests.integration import test_sync_activity_db as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged
from tests.integration.test_sync_manual_requester_seat_db import (
    assert_conflict,
    conflicts_of,
    denials,
    lose_requester_seat,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity


async def lose_approver_role(activity, monkeypatch) -> None:
    async with activity.factory() as db:
        member = await db.scalar(
            select(ContextMember).where(
                ContextMember.context_id == UUID(activity.target["context_id"]),
                ContextMember.user_id == activity.store.users["member"],
            )
        )
        member.role = "viewer"
        await db.commit()


async def lose_approver_reach(activity, monkeypatch) -> None:
    async with activity.factory() as db:
        member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == activity.store.wid,
                WorkspaceMember.user_id == activity.store.users["member"],
            )
        )
        member.allowed_context_ids = []
        await db.commit()


LOSSES = {"seat": lose_requester_seat, "role": lose_approver_role, "reach": lose_approver_reach}


@pytest.mark.parametrize("loss", list(LOSSES))
async def test_worker_commit_answers_409_when_the_approver_lost_authority(
    activity, monkeypatch, loss
):
    await committed(activity, [product()])
    row = await staged(activity, [])
    assert row["requires_approval"] is True
    approval = await human(
        activity,
        "POST",
        f"runs/{row['run_id']}/approvals",
        role="member",
        body={"diff_hash": row["diff_hash"]},
    )
    assert approval.status_code == 200, approval.text
    before = await denials(activity)

    await LOSSES[loss](activity, monkeypatch)
    seen = conflicts_of(monkeypatch, feed_routes, "commit_run")

    response = await service(
        activity,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={"diff_hash": row["diff_hash"], "approval_id": approval.json()["approval_id"]},
    )
    assert_conflict(response)
    assert seen == ["Approval authority changed"]
    # The approver's lost authority is not a refusal of the worker.
    assert await denials(activity) == before
    async with activity.factory() as db:
        current = await db.get(SyncRun, UUID(row["run_id"]))
        assert current.state == "prepared"
