"""``DELETE /admin/users/{id}`` answers a lock conflict with 409 (#2418).

A ``NOWAIT`` lock lost to another transaction (``55P03``) and a deadlock this
request was the victim of (``40P01``) are the same "the rows are busy": the
transaction is rolled back with nothing committed, and the deletion can run
again. Any other database error is passed on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from api.routes.admin import delete_user


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, SimpleNamespace(sqlstate=sqlstate))


def _db() -> MagicMock:
    return MagicMock(commit=AsyncMock(), rollback=AsyncMock())


async def _delete_failing_with(db: MagicMock, exc: BaseException, finish: AsyncMock) -> None:
    with (
        patch(
            "services.system_admin_service.SystemAdminService.can_delete_admin",
            AsyncMock(return_value=(True, "")),
        ),
        patch(
            "services.user_deletion_service.transfer_user_ownership",
            AsyncMock(return_value={}),
        ),
        patch("services.user_deletion_service.delete_user_rows", AsyncMock(side_effect=exc)),
        patch("services.user_deletion_service.finish_user_deletion", finish),
    ):
        await delete_user("target", {"user_id": "operator"}, db)


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["55P03", "40P01"])
async def test_a_lock_conflict_answers_409_after_the_rollback(sqlstate):
    db, finish = _db(), AsyncMock()
    with pytest.raises(HTTPException) as error:
        await _delete_failing_with(db, _db_error(sqlstate), finish)
    assert error.value.status_code == 409
    assert error.value.detail == "Workspace is being modified; retry deletion"
    assert isinstance(error.value.__cause__, DBAPIError)
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_any_other_database_error_is_passed_on():
    with pytest.raises(DBAPIError):
        await _delete_failing_with(_db(), _db_error("23505"), AsyncMock())
