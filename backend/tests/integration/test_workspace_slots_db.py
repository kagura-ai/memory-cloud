"""Workspace slot suspension on real PostgreSQL (#2918).

The read-time rule: a suspension row counts only while the owner it was
stamped with still owns the live workspace and the workspace is not under a
billing contract. Rows left behind by an ownership move, a deletion or a mode
switch are inert; a contracted workspace is never reported suspended.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension
from services.workspace_slots import owner_selection_required, suspended_workspace_ids

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = datetime(2031, 5, 1, 12, 0, 0)


def _mode(mode: str):
    return patch(
        "config.settings.get_settings",
        return_value=SimpleNamespace(workspace_slot_suspension=mode),
    )


async def _workspace(
    db: AsyncSession,
    owner: str,
    *,
    plan: str = "free",
    source: str = "admin_grant",
    deleted: bool = False,
) -> UUID:
    wid = uuid4()
    db.add(
        Workspace(
            id=wid,
            name=f"slots-{wid.hex[:8]}",
            owner_user_id=owner,
            plan_name=plan,
            entitlement_source=source,
            deleted_at=T0 if deleted else None,
        )
    )
    await db.flush()
    return wid


def _suspend(db: AsyncSession, wid: UUID, owner: str) -> None:
    db.add(
        WorkspaceSlotSuspension(
            workspace_id=wid, owner_user_id=owner, cause="billing_snapshot", suspended_at=T0
        )
    )


class TestReadTimeRule:
    async def test_only_rows_of_the_current_owner_on_live_uncontracted_workspaces_count(
        self, db_session: AsyncSession
    ) -> None:
        owner = f"slot-owner-{uuid4().hex[:8]}"
        other = f"slot-other-{uuid4().hex[:8]}"
        suspended = await _workspace(db_session, owner)
        active = await _workspace(db_session, owner)
        moved = await _workspace(db_session, owner)  # row stamped for the previous owner
        deleted = await _workspace(db_session, owner, deleted=True)
        contracted = await _workspace(db_session, owner, plan="pro", source="external_billing")
        granted = await _workspace(db_session, owner, plan="pro", source="admin_grant")
        _suspend(db_session, suspended, owner)
        _suspend(db_session, moved, other)
        _suspend(db_session, deleted, owner)
        _suspend(db_session, contracted, owner)
        _suspend(db_session, granted, owner)
        await db_session.flush()

        ids = [suspended, active, moved, deleted, contracted, granted]
        with _mode("enforce"):
            assert await suspended_workspace_ids(db_session, ids) == {suspended, granted}
        for mode in ("off", "shadow"):
            with _mode(mode):
                assert await suspended_workspace_ids(db_session, ids) == frozenset()

    async def test_selection_required_needs_the_flag_and_a_suspended_workspace(
        self, db_session: AsyncSession
    ) -> None:
        owner = f"slot-owner-{uuid4().hex[:8]}"
        wid = await _workspace(db_session, owner)
        with _mode("enforce"):
            assert await owner_selection_required(db_session, owner) is False

            db_session.add(
                WorkspaceSlotState(owner_user_id=owner, selection_required_since=T0, updated_at=T0)
            )
            await db_session.flush()
            # Flag set, nothing suspended.
            assert await owner_selection_required(db_session, owner) is False

            _suspend(db_session, wid, owner)
            await db_session.flush()
            assert await owner_selection_required(db_session, owner) is True

        with _mode("shadow"):
            assert await owner_selection_required(db_session, owner) is False

        state = await db_session.get(WorkspaceSlotState, owner)
        assert state is not None
        assert (state.keep_workspace_ids, state.version) == ([], 0)
        state.selection_required_since = None
        await db_session.flush()
        with _mode("enforce"):
            assert await owner_selection_required(db_session, owner) is False
