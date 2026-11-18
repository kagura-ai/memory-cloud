"""A manual requester who lost a seat answers 409 on every path, never 403 (#2912).

The worker's create / stage / prepare / commit of a manual run and another
Editor's resume preview / diff / confirm of a candidate pinned from a manual
request recheck the ORIGINAL requester. A lost seat of that requester is the
fixed ``RES-002`` conflict and records no ``sync.authority.denied`` against the
caller (the worker or the other Editor).
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from config import plan_tiers
from models.auth import AuditLog, Workspace
from services import sync_requests
from tests.integration import test_sync_activity_db as fixtures
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import human, manifest, product, service, staged
from utils.exceptions import ConflictError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds, activity = fixtures.store, fixtures.worker, fixtures.feeds, fixtures.activity
managed = management.managed


async def manual_request(activity) -> str:
    response = await human(
        activity, "POST", "activity/requests", role="member", body={"operation_id": str(uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()["request_id"]


async def lose_requester_seat(activity, monkeypatch) -> None:
    """The plan's seats shrink below the member count.

    The requester keeps the membership row and Editor role but no longer has a
    seat; the owner keeps one. The plan's features stay as they are (switching
    to a smaller plan would also end the installation's own entitlement and
    refuse the worker for a reason of its own), so the seat cap is lowered.
    """
    async with activity.factory() as db:
        workspace = await db.get(Workspace, activity.store.wid)
        tier = plan_tiers.get_plan_tier(workspace.plan_name)
        workspace.addon_member_bonus = 0
        await db.commit()
    key = next(name for name, plan in plan_tiers.PLAN_TIERS.items() if plan is tier)
    monkeypatch.setitem(plan_tiers.PLAN_TIERS, key, replace(tier, max_members_per_workspace=1))


async def denials(activity, *, user_id=None) -> int:
    # Any resource: a denial without a resolved installation is "sync-denial".
    query = (
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "sync.authority.denied")
    )
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    async with activity.factory() as db:
        return await db.scalar(query)


def assert_conflict(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "RES-002"}


def conflicts_of(monkeypatch, module, name) -> list[str]:
    """Record the conflict message each call of ``module.name`` ends with.

    The response only carries ``RES-002``; the message shows which check
    refused (here: the recheck of the original requester or approver).
    """
    seen: list[str] = []
    original = getattr(module, name)

    async def spy(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except ConflictError as exc:
            seen.append(exc.message)
            raise

    monkeypatch.setattr(module, name, spy)
    return seen


CHANGED = "Manual request authority changed"


async def test_worker_manual_run_paths_answer_409_when_the_requester_lost_a_seat(
    activity, monkeypatch
):
    records = [product()]
    to_stage = await service(
        activity,
        "POST",
        "runs",
        body=manifest(activity, records, manual_request_id=await manual_request(activity)),
    )
    assert to_stage.status_code == 201, to_stage.text
    to_prepare = await service(
        activity,
        "POST",
        "runs",
        body=manifest(activity, records, manual_request_id=await manual_request(activity)),
    )
    assert to_prepare.status_code == 201, to_prepare.text
    staged_chunk = await service(
        activity,
        "PUT",
        f"runs/{to_prepare.json()['run_id']}/chunks/0",
        body={"records": records},
    )
    assert staged_chunk.status_code == 200, staged_chunk.text
    to_commit = await staged(activity, records, manual_request_id=await manual_request(activity))
    unused_request = await manual_request(activity)
    before = await denials(activity)

    await lose_requester_seat(activity, monkeypatch)
    seen = conflicts_of(monkeypatch, sync_requests, "validate_manual_request")

    created = await service(
        activity,
        "POST",
        "runs",
        body=manifest(activity, records, manual_request_id=unused_request),
    )
    assert_conflict(created)
    assert seen == [CHANGED]
    assert_conflict(
        await service(
            activity,
            "PUT",
            f"runs/{to_stage.json()['run_id']}/chunks/0",
            body={"records": records},
        )
    )
    assert seen == [CHANGED] * 2
    assert_conflict(await service(activity, "POST", f"runs/{to_prepare.json()['run_id']}/prepare"))
    assert seen == [CHANGED] * 3
    assert_conflict(
        await service(
            activity,
            "POST",
            f"runs/{to_commit['run_id']}/commit",
            body={"diff_hash": to_commit["diff_hash"]},
        )
    )
    assert seen == [CHANGED] * 4
    # A lost original seat is a conflict, never a refusal of the worker.
    assert await denials(activity) == before


async def test_resume_by_another_editor_answers_409_when_the_requester_lost_a_seat(
    activity, managed, monkeypatch
):
    await management.manual(managed)
    request_id = await manual_request(activity)
    await management.mint(managed)
    row = await staged(managed, [product()], manual_request_id=request_id)
    pinned = await service(managed, "POST", "resume-candidates", body={"run_id": row["run_id"]})
    assert pinned.status_code == 200, pinned.text
    preview = await human(activity, "POST", "management/resume/preview")
    assert preview.status_code == 200, preview.text
    owner = activity.store.users["owner"]
    before = await denials(activity, user_id=owner)

    await lose_requester_seat(activity, monkeypatch)

    assert_conflict(
        await human(
            activity,
            "GET",
            "management/resume/diff",
            params={"preview_id": preview.json()["preview_id"]},
        )
    )
    assert_conflict(
        await human(
            activity,
            "POST",
            "management/resume/confirm",
            body={
                "operation_id": str(uuid4()),
                "preview_id": preview.json()["preview_id"],
                "diff_hash": preview.json()["diff_hash"],
            },
        )
    )
    assert_conflict(await human(activity, "POST", "management/resume/preview"))
    # The requester's seat is not this Editor's refusal: no denial recorded.
    assert await denials(activity, user_id=owner) == before
