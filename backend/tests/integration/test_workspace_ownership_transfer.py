"""Integration tests for WorkspaceOwnershipService.transfer_ownership (#1094).

These exercise the transactional core against a real Postgres session (the row
lock, single-owner invariant, epoch bump, role flip, audit row, idempotency, and
the TOCTOU re-check). They require the DB container (run via
``make test-integration``); they are not part of the DB-free unit suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from services.workspace_ownership_service import (
    OWNERSHIP_TRANSFER_ACTION,
    WorkspaceOwnershipService,
)
from utils.exceptions import BadRequestError, ConflictError


async def _add_user(db: AsyncSession, uid: str) -> None:
    db.add(
        User(
            email=f"{uid}@transfer.invalid",
            user_id=uid,
            name="Transfer Test",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )


async def _add_member(db: AsyncSession, workspace_id, uid: str, role: WorkspaceRole) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=uid, role=role))


@pytest_asyncio.fixture(loop_scope="session")
async def ws_fixture(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Owner + member + outsider, a workspace owned by owner, with member rows.

    Roles: owner=OWNER, member=MEMBER. ``outsider`` is a real user but NOT a
    member of the workspace.
    """
    owner = f"own_{uuid4().hex[:8]}"
    member = f"mem_{uuid4().hex[:8]}"
    outsider = f"out_{uuid4().hex[:8]}"
    workspace_id = uuid4()

    for uid in (owner, member, outsider):
        await _add_user(db_session, uid)
    await db_session.flush()

    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"xfer-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    await db_session.flush()
    await _add_member(db_session, workspace_id, owner, WorkspaceRole.OWNER)
    await _add_member(db_session, workspace_id, member, WorkspaceRole.MEMBER)
    await db_session.commit()

    yield SimpleNamespace(owner=owner, member=member, outsider=outsider, workspace_id=workspace_id)

    await db_session.execute(
        AuditLog.__table__.delete().where(AuditLog.resource == f"workspace:{workspace_id}")
    )
    await db_session.execute(
        WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == workspace_id)
    )
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(
        User.__table__.delete().where(User.user_id.in_([owner, member, outsider]))
    )
    await db_session.commit()


async def _role_of(db: AsyncSession, workspace_id, uid: str) -> WorkspaceRole | None:
    row = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == uid,
            )
        )
    ).scalar_one_or_none()
    return None if row is None else row.role


async def _workspace(db: AsyncSession, workspace_id) -> Workspace:
    return (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()


class TestOwnershipTransfer:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_transfer_promotes_demotes_bumps_epoch_and_audits(self, ws_fixture, db_session):
        s = ws_fixture
        result = await WorkspaceOwnershipService(db_session).transfer_ownership(
            workspace_id=s.workspace_id,
            current_owner_id=s.owner,
            target_user_id=s.member,
            performed_by_email="owner@transfer.invalid",
        )

        assert result.changed is True
        assert result.previous_owner_id == s.owner
        assert result.new_owner_id == s.member
        assert result.ownership_epoch == 1

        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.member
        assert ws.ownership_epoch == 1
        assert await _role_of(db_session, s.workspace_id, s.member) == WorkspaceRole.OWNER
        assert await _role_of(db_session, s.workspace_id, s.owner) == WorkspaceRole.ADMIN

        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.resource == f"workspace:{s.workspace_id}",
                        AuditLog.action == OWNERSHIP_TRANSFER_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].old_value_hash == s.owner
        assert audit[0].new_value_hash == s.member
        assert audit[0].user_metadata["ownership_epoch"] == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_idempotent_when_target_already_owner(self, ws_fixture, db_session):
        s = ws_fixture
        # Owner transfers to themselves → no-op, no epoch bump, no audit.
        result = await WorkspaceOwnershipService(db_session).transfer_ownership(
            workspace_id=s.workspace_id,
            current_owner_id=s.owner,
            target_user_id=s.owner,
            performed_by_email="owner@transfer.invalid",
        )
        assert result.changed is False
        assert result.ownership_epoch == 0

        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.owner
        assert ws.ownership_epoch == 0
        assert await _role_of(db_session, s.workspace_id, s.owner) == WorkspaceRole.OWNER
        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.resource == f"workspace:{s.workspace_id}")
                )
            )
            .scalars()
            .all()
        )
        assert audit == []

    @pytest.mark.asyncio(loop_scope="session")
    async def test_repeat_transfer_to_same_owner_is_noop(self, ws_fixture, db_session):
        s = ws_fixture
        svc = WorkspaceOwnershipService(db_session)
        first = await svc.transfer_ownership(
            workspace_id=s.workspace_id,
            current_owner_id=s.owner,
            target_user_id=s.member,
            performed_by_email="owner@transfer.invalid",
        )
        assert first.changed is True and first.ownership_epoch == 1

        # Now member is owner; transferring to member again is the no-op path.
        second = await svc.transfer_ownership(
            workspace_id=s.workspace_id,
            current_owner_id=s.member,
            target_user_id=s.member,
            performed_by_email="member@transfer.invalid",
        )
        assert second.changed is False
        assert second.ownership_epoch == 1  # not bumped again
        assert (await _workspace(db_session, s.workspace_id)).ownership_epoch == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_target_not_member_raises_bad_request_and_no_change(self, ws_fixture, db_session):
        s = ws_fixture
        with pytest.raises(BadRequestError):
            await WorkspaceOwnershipService(db_session).transfer_ownership(
                workspace_id=s.workspace_id,
                current_owner_id=s.owner,
                target_user_id=s.outsider,  # real user, NOT a member
                performed_by_email="owner@transfer.invalid",
            )
        await db_session.rollback()
        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.owner
        assert ws.ownership_epoch == 0

    @pytest.mark.asyncio(loop_scope="session")
    async def test_stale_current_owner_raises_conflict(self, ws_fixture, db_session):
        s = ws_fixture
        # The route's owner check passed for a caller who is no longer the owner
        # (raced). The under-lock re-check must reject with 409.
        with pytest.raises(ConflictError):
            await WorkspaceOwnershipService(db_session).transfer_ownership(
                workspace_id=s.workspace_id,
                current_owner_id=s.member,  # NOT the current owner
                target_user_id=s.member,
                performed_by_email="member@transfer.invalid",
            )
        await db_session.rollback()
        assert (await _workspace(db_session, s.workspace_id)).owner_user_id == s.owner

    @pytest.mark.asyncio(loop_scope="session")
    async def test_missing_previous_owner_member_row_still_transfers(self, db_session):
        # Legacy shape: owner_user_id set, but the owner has NO WorkspaceMember row.
        owner = f"legown_{uuid4().hex[:8]}"
        member = f"legmem_{uuid4().hex[:8]}"
        workspace_id = uuid4()
        for uid in (owner, member):
            await _add_user(db_session, uid)
        await db_session.flush()
        db_session.add(
            Workspace(
                id=workspace_id,
                name=f"legacy-{uuid4().hex[:8]}",
                plan_name="free",
                owner_user_id=owner,
                daily_api_limit=100,
                weekly_api_limit=500,
                deleted_at=None,
            )
        )
        await db_session.flush()
        await _add_member(db_session, workspace_id, member, WorkspaceRole.MEMBER)  # only the member
        await db_session.commit()
        try:
            result = await WorkspaceOwnershipService(db_session).transfer_ownership(
                workspace_id=workspace_id,
                current_owner_id=owner,
                target_user_id=member,
                performed_by_email="legacy@transfer.invalid",
            )
            assert result.changed is True
            ws = await _workspace(db_session, workspace_id)
            assert ws.owner_user_id == member
            assert ws.ownership_epoch == 1
            assert await _role_of(db_session, workspace_id, member) == WorkspaceRole.OWNER
        finally:
            await db_session.execute(
                AuditLog.__table__.delete().where(AuditLog.resource == f"workspace:{workspace_id}")
            )
            await db_session.execute(
                WorkspaceMember.__table__.delete().where(
                    WorkspaceMember.workspace_id == workspace_id
                )
            )
            await db_session.execute(
                Workspace.__table__.delete().where(Workspace.id == workspace_id)
            )
            await db_session.execute(
                User.__table__.delete().where(User.user_id.in_([owner, member]))
            )
            await db_session.commit()
