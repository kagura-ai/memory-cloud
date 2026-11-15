"""``GET /api/v1/admin/plans/audit``: workspace filter and paging on real rows (#2830)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import PlanChange, Workspace

from ._admin_helpers import make_user, make_workspace
from ._admin_http import admin_http

pytestmark = pytest.mark.asyncio

AUDIT = "/api/v1/admin/plans/audit"
ENTRY_KEYS = {
    "id",
    "workspace_id",
    "workspace_name",
    "old_plan",
    "new_plan",
    "changed_by",
    "changed_by_name",
    "changed_by_email",
    "changed_at",
    "reason",
}


async def _workspace(db_session: AsyncSession, *, soft_deleted: bool = False) -> Workspace:
    user = make_user(name="Audit Owner")
    db_session.add(user)
    await db_session.flush()
    workspace = make_workspace(owner_user_id=user.user_id, soft_deleted=soft_deleted)
    db_session.add(workspace)
    await db_session.flush()
    return workspace


def _change(workspace: Workspace, changed_at: datetime, reason: str) -> PlanChange:
    return PlanChange(
        workspace_id=workspace.id,
        old_plan="free",
        new_plan="pro",
        changed_by="admin_runner",
        changed_at=changed_at,
        reason=reason,
    )


async def test_workspace_filter_answers_only_its_rows_newest_first_ties_by_id_descending(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session)
    other = await _workspace(db_session)
    same = datetime(2030, 3, 3, 9, 0, 0)
    mine = [
        _change(workspace, same - timedelta(hours=1), "older"),
        _change(workspace, same, "tie-a"),
        _change(workspace, same, "tie-b"),
        _change(workspace, same, "tie-c"),
        _change(workspace, same + timedelta(hours=1), "newer"),
    ]
    db_session.add_all([*mine, _change(other, same, "other workspace")])
    await db_session.commit()

    async with admin_http(db_session) as client:
        response = await client.get(AUDIT, params={"workspace_id": str(workspace.id)})

    assert response.status_code == 200, response.text
    entries = response.json()
    assert isinstance(entries, list)
    assert {entry["workspace_id"] for entry in entries} == {str(workspace.id)}
    assert set(entries[0]) == ENTRY_KEYS
    tie_ids = sorted((change.id for change in mine[1:4]), reverse=True)
    assert [entry["id"] for entry in entries] == [mine[4].id, *tie_ids, mine[0].id]
    assert [entry["reason"] for entry in entries][0] == "newer"


async def test_workspace_filter_includes_a_soft_deleted_workspace(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session, soft_deleted=True)
    db_session.add(_change(workspace, datetime(2030, 1, 1), "before the delete"))
    await db_session.commit()

    async with admin_http(db_session) as client:
        response = await client.get(AUDIT, params={"workspace_id": str(workspace.id)})

    assert [entry["reason"] for entry in response.json()] == ["before the delete"]


async def test_workspace_without_rows_answers_an_empty_array(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    await db_session.commit()

    async with admin_http(db_session) as client:
        empty = await client.get(AUDIT, params={"workspace_id": str(workspace.id)})
        unknown = await client.get(AUDIT, params={"workspace_id": str(uuid4())})

    assert (empty.status_code, empty.json()) == (200, [])
    assert (unknown.status_code, unknown.json()) == (200, [])


async def test_paging_returns_every_row_once_when_changed_at_is_shared(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session)
    same = datetime(2030, 4, 4, 9, 0, 0)
    changes = [_change(workspace, same, f"tie-{index}") for index in range(7)]
    db_session.add_all(changes)
    await db_session.commit()

    seen: list[int] = []
    async with admin_http(db_session) as client:
        # A caller pages until a page holds fewer rows than ``limit``.
        for offset in range(0, 30, 3):
            response = await client.get(
                AUDIT, params={"workspace_id": str(workspace.id), "limit": 3, "offset": offset}
            )
            assert response.status_code == 200, response.text
            page = response.json()
            seen += [entry["id"] for entry in page]
            if len(page) < 3:
                break
        else:
            pytest.fail("the last page never came")

    assert seen == sorted((change.id for change in changes), reverse=True)


async def test_without_the_new_parameters_the_newest_rows_of_all_workspaces_are_answered(
    db_session: AsyncSession,
) -> None:
    """Rows dated after anything another test writes, so they lead the array."""
    first = await _workspace(db_session)
    second = await _workspace(db_session, soft_deleted=True)
    base = datetime(2099, 1, 1)
    changes = [
        _change(first, base, "a"),
        _change(second, base + timedelta(minutes=1), "b"),
        _change(first, base + timedelta(minutes=2), "c"),
    ]
    db_session.add_all(changes)
    await db_session.commit()

    async with admin_http(db_session) as client:
        default = await client.get(AUDIT)
        limited = await client.get(AUDIT, params={"limit": 2})

    assert default.status_code == 200, default.text
    entries = default.json()
    assert isinstance(entries, list)
    assert len(entries) <= 100
    assert [entry["reason"] for entry in entries[:3]] == ["c", "b", "a"]
    assert [entry["reason"] for entry in limited.json()] == ["c", "b"]
