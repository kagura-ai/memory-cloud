"""Running-subscription checks shared by the deletion paths (Issue #1940).

A workspace is under a billing contract while the external billing service owns
its entitlement on a paid tier — ``Workspace.has_active_billing_contract`` is the
single predicate. This module holds the queries built on it so the workspace,
account-erasure and admin paths (and the Free-plan follow-ups) read the same
rule instead of re-deriving it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from utils.exceptions import BillingContractActiveError


async def owned_workspaces_under_contract(db: AsyncSession, user_id: str) -> list[Workspace]:
    """Live workspaces ``user_id`` owns that have a running subscription.

    Every owned workspace counts, including one that account erasure would hand
    to another admin: the subscription is the erased user's, so it must end
    before the account goes. A user owns a handful of workspaces at most, so the
    rows are filtered in Python with the model predicate rather than repeating
    it in SQL.

    Args:
        db: Async database session.
        user_id: Owner whose workspaces are checked.

    Returns:
        The matching workspaces (empty when none is under contract).
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.owner_user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
    )
    return [ws for ws in result.scalars().all() if ws.has_active_billing_contract]


def ensure_no_billing_contract(workspaces: list[Workspace]) -> None:
    """Raise ``BillingContractActiveError`` if any workspace is under contract.

    Args:
        workspaces: Workspaces about to be deleted (directly or with their owner).

    Raises:
        BillingContractActiveError: At least one has a running subscription
            (409 ``BILLING-005``); ``details.workspace_ids`` lists them.
    """
    under_contract = [str(ws.id) for ws in workspaces if ws.has_active_billing_contract]
    if under_contract:
        raise BillingContractActiveError(workspace_ids=under_contract)
