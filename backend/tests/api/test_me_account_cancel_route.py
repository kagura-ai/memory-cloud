"""Route test for DELETE /me/account/erasure-request (#2306).

The cancel is a compare-and-set on the status (#2282). When it loses, the
row is read again as the caller's and the answer follows what it is now:

| row after the lost compare-and-set        | answer                          |
|-------------------------------------------|---------------------------------|
| ``cancelled`` (a concurrent cancel won)   | 200, the body a first cancel returns |
| ``in_progress``                           | 409 ``ERASURE-006``             |
| back in ``cooling_off`` (claimed, then rescheduled) | 409 ``in_progress``   |
| ``complete`` / ``failed``, gone, or no longer the caller's | 404 ``ERASURE-001`` |

DB-free: dependency overrides, the real service over a scripted session,
and the app's real ``MemoryCloudException`` handler, so the bodies are the
ones clients get.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from api.main import memory_cloud_exception_handler
from api.routes import me_account
from auth.dependencies import require_session_auth
from db.base import get_db
from models.erasure import (
    REASON_SELF_SERVICE,
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_COOLING_OFF,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    ErasureRequest,
)
from services.account_erasure_service import AccountErasureService
from utils.exceptions import MemoryCloudException

USER = "u-cancel-1"
_PATH = "/me/account/erasure-request"
_REQUESTED_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
_CANCELLED_AT = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _row(status: str = STATUS_COOLING_OFF) -> ErasureRequest:
    row = ErasureRequest(
        user_id=USER,
        user_email_hash="x",
        initiated_by=USER,
        is_self_service=True,
        reason_code=REASON_SELF_SERVICE,
        status=status,
    )
    row.id = uuid4()
    row.requested_at = _REQUESTED_AT
    row.confirmed_at = _REQUESTED_AT
    row.scheduled_for = _REQUESTED_AT + timedelta(days=7)
    return row


def _cancelled(row: ErasureRequest) -> ErasureRequest:
    row.status = STATUS_CANCELLED
    row.cancelled_at = _CANCELLED_AT
    return row


def _client(db: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(me_account.router)
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]

    async def _user():
        return {"user_id": USER}

    async def _db():
        yield db

    app.dependency_overrides[require_session_auth] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _session(*results) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _reread(row: ErasureRequest | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


def _lost_cas() -> MagicMock:
    """The cancel's compare-and-set result when it matched nothing: no
    ``RETURNING`` row (#2304)."""
    result = MagicMock(rowcount=0)
    result.first.return_value = None
    return result


def _cancel(db: MagicMock, read: ErasureRequest):
    with patch.object(
        AccountErasureService, "_find_active_request", new=AsyncMock(return_value=read)
    ):
        return _client(db).delete(_PATH)


def _lost(reread_row: ErasureRequest | None, read: ErasureRequest | None = None):
    """The cancel read ``cooling_off``; its compare-and-set matched nothing."""
    read = read or _row()
    db = _session(_lost_cas(), _reread(reread_row))
    return _cancel(db, read), db


def test_a_first_cancel_answers_200_with_the_cancelled_request():
    row = _row()
    db = _session(MagicMock(rowcount=1))
    db.refresh = AsyncMock(side_effect=_cancelled)

    resp = _cancel(db, row)

    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == str(row.id)
    assert body["status"] == STATUS_CANCELLED
    assert body["cancelled_at"] is not None


def test_a_cancel_that_loses_to_a_concurrent_cancel_answers_like_the_first():
    """Two cancels (e.g. two tabs) both read ``cooling_off``, one wins the
    compare-and-set; the other gets the same 200 body, not a 409."""
    first_row = _row()
    first_db = _session(MagicMock(rowcount=1))
    first_db.refresh = AsyncMock(side_effect=_cancelled)
    first = _cancel(first_db, first_row)

    second_read = _row()
    second_read.id = first_row.id
    stored = _cancelled(_row())
    stored.id = first_row.id
    second, db = _lost(stored, read=second_read)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    # the re-read is the caller's row by id and owner, refreshed from the DB
    update_stmt, reread_stmt = [c.args[0] for c in db.execute.await_args_list]
    assert isinstance(update_stmt, Update)
    assert isinstance(reread_stmt, Select)
    params = reread_stmt.compile().params
    assert params["id_1"] == first_row.id
    assert params["user_id_1"] == USER
    assert reread_stmt.get_execution_options()["populate_existing"] is True
    db.refresh.assert_not_awaited()


def test_a_row_gone_meanwhile_answers_404():
    resp, _db = _lost(None)

    assert resp.status_code == 404
    assert resp.json()["error"] == "ERASURE-001"


def test_a_row_claimed_meanwhile_answers_409_in_progress():
    resp, _db = _lost(_row(STATUS_IN_PROGRESS))

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "ERASURE-006"
    assert body["details"] == {"existing_status": STATUS_IN_PROGRESS}
    assert "already in_progress" in body["message"]


@pytest.mark.parametrize("status", [STATUS_COMPLETE, STATUS_FAILED])
def test_a_row_that_ended_answers_404(status):
    """Nothing left to cancel. (A finished erasure pseudonymizes the row's
    ``user_id``, so the re-read usually finds nothing; a ``failed`` row
    before the Postgres step keeps it.)"""
    resp, _db = _lost(_row(status))

    assert resp.status_code == 404
    assert resp.json()["error"] == "ERASURE-001"


def test_a_row_claimed_then_rescheduled_answers_409_in_progress():
    """The sweep claimed the row before the cancel's write and the run put it
    back to ``cooling_off`` (a vector-store retry, #2282) before the re-read:
    what beat the cancel was the claim."""
    resp, _db = _lost(_row(STATUS_COOLING_OFF))

    assert resp.status_code == 409
    assert resp.json()["details"] == {"existing_status": STATUS_IN_PROGRESS}


def test_no_cancellable_request_answers_404_without_a_write():
    db = _session()
    with patch.object(
        AccountErasureService, "_find_active_request", new=AsyncMock(return_value=None)
    ):
        resp = _client(db).delete(_PATH)

    assert resp.status_code == 404
    assert resp.json()["error"] == "ERASURE-001"
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
