"""Integration tests for dual-control break-glass force-transfer (#1113).

Exercises ``WorkspaceOwnershipService.initiate_force_transfer`` /
``approve_force_transfer`` / ``cancel_force_transfer`` against a real Postgres
session: the pending-request lifecycle, the four-eyes (no self-approval) rule,
the epoch-staleness re-check at approval, supersede-on-initiate, and that the
refactored single-control ``force_transfer_ownership`` (#1101) still behaves
exactly as before. Requires the DB container (``make test-integration``).
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
from models.auth import (
    FORCE_TRANSFER_STATUS_APPROVED,
    FORCE_TRANSFER_STATUS_CANCELLED,
    FORCE_TRANSFER_STATUS_PENDING,
    FORCE_TRANSFER_STATUS_SUPERSEDED,
    AuditLog,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceOwnershipForceTransferRequest,
)
from services.workspace_ownership_service import (
    FORCE_OWNERSHIP_TRANSFER_ACTION,
    WorkspaceOwnershipService,
)
from utils.exceptions import BadRequestError, ConflictError, NotFoundException


async def _add_user(db: AsyncSession, uid: str) -> None:
    db.add(
        User(
            email=f"{uid}@dc.invalid",
            user_id=uid,
            name="Dual Control Test",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )


@pytest_asyncio.fixture(loop_scope="session")
async def dc_fixture(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Workspace owned by ``owner`` plus a ``target`` and two admins (a/b).

    ``admin_a`` initiates; ``admin_b`` is the distinct approver. ``target`` is a
    real user who is NOT a member (exercises the elevated add-as-OWNER path).
    """
    owner = f"own_{uuid4().hex[:8]}"
    target = f"tgt_{uuid4().hex[:8]}"
    admin_a = f"ada_{uuid4().hex[:8]}"
    admin_b = f"adb_{uuid4().hex[:8]}"
    workspace_id = uuid4()

    for uid in (owner, target, admin_a, admin_b):
        await _add_user(db_session, uid)
    await db_session.flush()

    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"dc-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace_id, user_id=owner, role=WorkspaceRole.OWNER)
    )
    await db_session.commit()

    yield SimpleNamespace(
        owner=owner, target=target, admin_a=admin_a, admin_b=admin_b, workspace_id=workspace_id
    )

    await db_session.execute(
        WorkspaceOwnershipForceTransferRequest.__table__.delete().where(
            WorkspaceOwnershipForceTransferRequest.workspace_id == workspace_id
        )
    )
    await db_session.execute(
        AuditLog.__table__.delete().where(AuditLog.resource == f"workspace:{workspace_id}")
    )
    await db_session.execute(
        WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == workspace_id)
    )
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(
        User.__table__.delete().where(User.user_id.in_([owner, target, admin_a, admin_b]))
    )
    await db_session.commit()


async def _workspace(db: AsyncSession, workspace_id) -> Workspace:
    return (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()


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


async def _pending(db: AsyncSession, workspace_id) -> list[WorkspaceOwnershipForceTransferRequest]:
    return list(
        (
            await db.execute(
                select(WorkspaceOwnershipForceTransferRequest).where(
                    WorkspaceOwnershipForceTransferRequest.workspace_id == workspace_id,
                    WorkspaceOwnershipForceTransferRequest.status == FORCE_TRANSFER_STATUS_PENDING,
                )
            )
        )
        .scalars()
        .all()
    )


class TestInitiate:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_initiate_creates_pending_without_transferring(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        assert req.status == FORCE_TRANSFER_STATUS_PENDING
        assert req.target_user_id == s.target
        assert req.initiated_by_user_id == s.admin_a
        assert req.ownership_epoch_at_initiation == 0

        # No transfer happened — ownership and epoch are untouched.
        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.owner
        assert ws.ownership_epoch == 0
        # No audit row yet (the transfer has not been applied).
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
    async def test_initiate_phantom_target_rejected(self, dc_fixture, db_session):
        s = dc_fixture
        with pytest.raises(BadRequestError):
            await WorkspaceOwnershipService(db_session).initiate_force_transfer(
                workspace_id=s.workspace_id,
                target_user_id="ghost_does_not_exist",
                performed_by_user_id=s.admin_a,
                performed_by_email="ada@dc.invalid",
                reason="owner unreachable",
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_initiate_supersedes_prior_pending(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        first = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="first",
        )
        second = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_b,
            performed_by_email="adb@dc.invalid",
            reason="second",
        )
        # Exactly one pending request — the second — and the first is superseded.
        pending = await _pending(db_session, s.workspace_id)
        assert [p.id for p in pending] == [second.id]
        await db_session.refresh(first)
        assert first.status == FORCE_TRANSFER_STATUS_SUPERSEDED
        assert first.decided_by_user_id == s.admin_b


class TestApprove:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_approve_by_distinct_admin_transfers_and_audits_both(
        self, dc_fixture, db_session
    ):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        result = await svc.approve_force_transfer(
            request_id=req.id,
            approver_user_id=s.admin_b,
            approver_email="adb@dc.invalid",
        )
        assert result.changed is True
        assert result.new_owner_id == s.target
        assert result.ownership_epoch == 1

        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.target
        assert ws.ownership_epoch == 1
        assert await _role_of(db_session, s.workspace_id, s.target) == WorkspaceRole.OWNER
        assert await _role_of(db_session, s.workspace_id, s.owner) == WorkspaceRole.ADMIN

        await db_session.refresh(req)
        assert req.status == FORCE_TRANSFER_STATUS_APPROVED
        assert req.decided_by_user_id == s.admin_b

        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.resource == f"workspace:{s.workspace_id}",
                        AuditLog.action == FORCE_OWNERSHIP_TRANSFER_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        meta = audit[0].user_metadata
        assert audit[0].user_id == s.admin_b  # actor = approver
        assert meta["break_glass"] is True
        assert meta["dual_control"] is True
        assert meta["initiated_by_user_id"] == s.admin_a
        assert meta["approved_by_user_id"] == s.admin_b
        assert meta["new_owner_id"] == s.target

    @pytest.mark.asyncio(loop_scope="session")
    async def test_self_approval_rejected(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        with pytest.raises(BadRequestError, match="different system admin"):
            await svc.approve_force_transfer(
                request_id=req.id,
                approver_user_id=s.admin_a,  # same as initiator
                approver_email="ada@dc.invalid",
            )
        # No transfer; request still pending.
        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.owner
        assert ws.ownership_epoch == 0
        await db_session.refresh(req)
        assert req.status == FORCE_TRANSFER_STATUS_PENDING

    @pytest.mark.asyncio(loop_scope="session")
    async def test_stale_epoch_rejected(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        # Simulate a concurrent ownership change between initiation and approval:
        # the epoch moves, so the snapshot is stale.
        ws = await _workspace(db_session, s.workspace_id)
        ws.ownership_epoch = ws.ownership_epoch + 1
        await db_session.commit()

        with pytest.raises(ConflictError, match="changed since"):
            await svc.approve_force_transfer(
                request_id=req.id,
                approver_user_id=s.admin_b,
                approver_email="adb@dc.invalid",
            )
        # Request remains pending (a fresh initiate is required to re-snapshot).
        await db_session.refresh(req)
        assert req.status == FORCE_TRANSFER_STATUS_PENDING

    @pytest.mark.asyncio(loop_scope="session")
    async def test_approve_non_pending_rejected(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        await svc.approve_force_transfer(
            request_id=req.id, approver_user_id=s.admin_b, approver_email="adb@dc.invalid"
        )
        # A second approval of the now-approved request is refused.
        with pytest.raises(ConflictError, match="not pending"):
            await svc.approve_force_transfer(
                request_id=req.id, approver_user_id=s.admin_b, approver_email="adb@dc.invalid"
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_approve_unknown_request_404(self, dc_fixture, db_session):
        with pytest.raises(NotFoundException):
            await WorkspaceOwnershipService(db_session).approve_force_transfer(
                request_id=uuid4(),
                approver_user_id=dc_fixture.admin_b,
                approver_email="adb@dc.invalid",
            )


class TestCancel:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_cancel_pending_then_approve_refused(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="filed in error",
        )
        cancelled = await svc.cancel_force_transfer(
            request_id=req.id,
            cancelled_by_user_id=s.admin_a,
            cancelled_by_email="ada@dc.invalid",
        )
        assert cancelled.status == FORCE_TRANSFER_STATUS_CANCELLED
        assert cancelled.decided_by_user_id == s.admin_a

        with pytest.raises(ConflictError, match="not pending"):
            await svc.approve_force_transfer(
                request_id=req.id, approver_user_id=s.admin_b, approver_email="adb@dc.invalid"
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_cancel_non_pending_rejected(self, dc_fixture, db_session):
        s = dc_fixture
        svc = WorkspaceOwnershipService(db_session)
        req = await svc.initiate_force_transfer(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="owner unreachable",
        )
        await svc.approve_force_transfer(
            request_id=req.id, approver_user_id=s.admin_b, approver_email="adb@dc.invalid"
        )
        with pytest.raises(ConflictError):
            await svc.cancel_force_transfer(
                request_id=req.id,
                cancelled_by_user_id=s.admin_a,
                cancelled_by_email="ada@dc.invalid",
            )


class TestSingleControlRegression:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_ownership_unchanged_by_refactor(self, dc_fixture, db_session):
        # The refactored #1101 single-control path must still transfer + audit,
        # WITHOUT any dual_control metadata.
        s = dc_fixture
        result = await WorkspaceOwnershipService(db_session).force_transfer_ownership(
            workspace_id=s.workspace_id,
            target_user_id=s.target,
            performed_by_user_id=s.admin_a,
            performed_by_email="ada@dc.invalid",
            reason="break glass",
        )
        assert result.changed is True
        assert result.new_owner_id == s.target
        assert result.ownership_epoch == 1

        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.target
        assert await _role_of(db_session, s.workspace_id, s.target) == WorkspaceRole.OWNER

        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.resource == f"workspace:{s.workspace_id}",
                        AuditLog.action == FORCE_OWNERSHIP_TRANSFER_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].user_metadata["break_glass"] is True
        assert "dual_control" not in audit[0].user_metadata
