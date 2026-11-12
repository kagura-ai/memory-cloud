"""Four-eyes on a force transfer holds across SYSTEM_ADMIN_MODE principals (#2767).

While a deployment moves from ``session`` to ``service`` both share one
database: a person could file a request as a session admin (bare address) and
approve it from the operator console as ``operator:<same address>``. The
approver is refused when the bare addresses match, case-insensitively, in
both directions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.auth import FORCE_TRANSFER_STATUS_PENDING
from services.workspace_ownership_service import WorkspaceOwnershipService
from utils.exceptions import BadRequestError


class _PassedTheCheck(Exception):
    pass


def _service(*, initiated_by_user_id: str, initiated_by_email: str) -> WorkspaceOwnershipService:
    request = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        target_user_id="target-1",
        status=FORCE_TRANSFER_STATUS_PENDING,
        initiated_by_user_id=initiated_by_user_id,
        initiated_by_email=initiated_by_email,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = request
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    svc = WorkspaceOwnershipService(db)
    svc._lock_transfer_workspace = AsyncMock(side_effect=_PassedTheCheck)  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initiator", "approver"),
    [
        # session admin files, the same person approves from the console
        (
            ("1001", "Alice@Example.com"),
            ("operator:alice@example.com", "operator:alice@example.com"),
        ),
        # the console files, the same person approves in the in-app page
        (
            ("operator:alice@example.com", "operator:alice@example.com"),
            ("1001", "alice@example.com"),
        ),
        # same principal, as before
        (("1001", "alice@example.com"), ("1001", "alice@example.com")),
        (
            ("operator:alice@example.com", "operator:alice@example.com"),
            ("operator:alice@example.com", "operator:alice@example.com"),
        ),
    ],
)
async def test_the_same_person_cannot_approve(initiator, approver):
    svc = _service(initiated_by_user_id=initiator[0], initiated_by_email=initiator[1])
    with pytest.raises(BadRequestError) as info:
        await svc.approve_force_transfer(
            request_id=uuid4(), approver_user_id=approver[0], approver_email=approver[1]
        )
    assert info.value.error_code == "WS-OWNER-DUAL-001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initiator", "approver"),
    [
        (("1001", "alice@example.com"), ("operator:bob@example.com", "operator:bob@example.com")),
        (
            ("operator:alice@example.com", "operator:alice@example.com"),
            ("operator:bob@example.com", "operator:bob@example.com"),
        ),
        (("1001", "alice@example.com"), ("1002", "bob@example.com")),
    ],
)
async def test_two_people_pass_the_check(initiator, approver):
    svc = _service(initiated_by_user_id=initiator[0], initiated_by_email=initiator[1])
    with pytest.raises(_PassedTheCheck):
        await svc.approve_force_transfer(
            request_id=uuid4(), approver_user_id=approver[0], approver_email=approver[1]
        )
