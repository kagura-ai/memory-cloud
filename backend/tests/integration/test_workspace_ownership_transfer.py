"""Integration tests for WorkspaceOwnershipService.transfer_ownership (#1094).

These exercise the transactional core against a real Postgres session (the row
lock, single-owner invariant, epoch bump, role flip, audit row, idempotency, and
the TOCTOU re-check). They require the DB container (run via
``make test-integration``); they are not part of the DB-free unit suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from services.workspace_ownership_service import (
    FORCE_OWNERSHIP_TRANSFER_ACTION,
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
        assert audit[0].user_id == s.owner  # actor = the performing owner
        assert audit[0].user_metadata["previous_owner_id"] == s.owner
        assert audit[0].user_metadata["new_owner_id"] == s.member
        assert audit[0].user_metadata["ownership_epoch"] == 1
        # The actor lives in the first-class AuditLog.user_id column (asserted
        # above); it must NOT be duplicated into user_metadata as "performed_by".
        assert "performed_by" not in audit[0].user_metadata

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
        # A rejected attempt writes NO audit row.
        leftover = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.resource == f"workspace:{s.workspace_id}")
                )
            )
            .scalars()
            .all()
        )
        assert leftover == []

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
        leftover = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.resource == f"workspace:{s.workspace_id}")
                )
            )
            .scalars()
            .all()
        )
        assert leftover == []

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
            # Audit trail is intact even on the legacy (no previous-member) path.
            audit = (
                (
                    await db_session.execute(
                        select(AuditLog).where(AuditLog.resource == f"workspace:{workspace_id}")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audit) == 1
            assert audit[0].user_metadata["new_owner_id"] == member
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

    @pytest.mark.asyncio(loop_scope="session")
    async def test_concurrent_transfers_preserve_single_owner(self, async_engine, db_session):
        # THE acceptance criterion: single-owner invariant preserved under
        # concurrency (row lock). Two simultaneous transfers from the same owner
        # to two DIFFERENT members race on independent sessions. The workspace
        # FOR UPDATE lock must serialize them: exactly one commits, the loser's
        # under-lock owner re-check fails with 409 — never two OWNER rows, never a
        # double epoch bump. (If the lock were dropped, both could read owner==A,
        # both pass the re-check, and both promote → this test would fail.)
        owner = f"cown_{uuid4().hex[:8]}"
        m1 = f"cm1_{uuid4().hex[:8]}"
        m2 = f"cm2_{uuid4().hex[:8]}"
        workspace_id = uuid4()
        for uid in (owner, m1, m2):
            await _add_user(db_session, uid)
        await db_session.flush()
        db_session.add(
            Workspace(
                id=workspace_id,
                name=f"conc-{uuid4().hex[:8]}",
                plan_name="pro",
                owner_user_id=owner,
                daily_api_limit=500,
                weekly_api_limit=2500,
                deleted_at=None,
            )
        )
        await db_session.flush()
        await _add_member(db_session, workspace_id, owner, WorkspaceRole.OWNER)
        await _add_member(db_session, workspace_id, m1, WorkspaceRole.MEMBER)
        await _add_member(db_session, workspace_id, m2, WorkspaceRole.MEMBER)
        await db_session.commit()

        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        async def _xfer(target: str) -> str:
            async with session_maker() as s:
                try:
                    await WorkspaceOwnershipService(s).transfer_ownership(
                        workspace_id=workspace_id,
                        current_owner_id=owner,
                        target_user_id=target,
                        performed_by_email="conc@transfer.invalid",
                    )
                    return "ok"
                except ConflictError:
                    return "conflict"

        try:
            statuses = sorted(await asyncio.gather(_xfer(m1), _xfer(m2)))
            assert statuses == ["conflict", "ok"], f"expected one ok + one conflict, got {statuses}"

            # Exactly one OWNER member row; owner_user_id agrees; epoch bumped once.
            async with session_maker() as s:
                ws = (
                    await s.execute(select(Workspace).where(Workspace.id == workspace_id))
                ).scalar_one()
                owners = (
                    (
                        await s.execute(
                            select(WorkspaceMember).where(
                                WorkspaceMember.workspace_id == workspace_id,
                                WorkspaceMember.role == WorkspaceRole.OWNER,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(owners) == 1
                assert owners[0].user_id == ws.owner_user_id
                assert ws.owner_user_id in (m1, m2)
                assert ws.ownership_epoch == 1
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
                User.__table__.delete().where(User.user_id.in_([owner, m1, m2]))
            )
            await db_session.commit()


class TestForceTransfer:
    """Break-glass admin force-transfer (#1101) — no current-owner check, may add
    a non-member target as OWNER, mandatory break_glass audit."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_existing_member_promotes_demotes_audits(
        self, ws_fixture, db_session
    ):
        s = ws_fixture
        # The admin actor is NOT the owner — break-glass has no current-owner check.
        result = await WorkspaceOwnershipService(db_session).force_transfer_ownership(
            workspace_id=s.workspace_id,
            target_user_id=s.member,
            performed_by_user_id="admin_runner",
            performed_by_email="admin@kagura.invalid",
            reason="owner unreachable for 30 days",
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
                        AuditLog.action == FORCE_OWNERSHIP_TRANSFER_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        meta = audit[0].user_metadata
        assert audit[0].user_id == "admin_runner"  # actor = the acting ADMIN
        assert meta["break_glass"] is True
        assert meta["reason"] == "owner unreachable for 30 days"
        assert meta["previous_owner_id"] == s.owner
        assert meta["new_owner_id"] == s.member
        assert meta["ownership_epoch"] == 1
        assert meta["target_was_member"] is True
        assert meta["self_transfer"] is False  # admin_runner != the target member

    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_adds_non_member_target_as_owner(self, ws_fixture, db_session):
        # The outsider is a real User but NOT a member — break-glass ADDS them as
        # OWNER (the voluntary path would reject this with 400).
        s = ws_fixture
        result = await WorkspaceOwnershipService(db_session).force_transfer_ownership(
            workspace_id=s.workspace_id,
            target_user_id=s.outsider,
            performed_by_user_id="admin_runner",
            performed_by_email="admin@kagura.invalid",
            reason="break glass to outsider",
        )
        assert result.changed is True
        assert result.new_owner_id == s.outsider

        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.outsider
        assert await _role_of(db_session, s.workspace_id, s.outsider) == WorkspaceRole.OWNER
        # The freshly-added OWNER member carries a join timestamp like every other
        # membership-creation path (not a NULL joined_at).
        new_member = (
            await db_session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == s.workspace_id,
                    WorkspaceMember.user_id == s.outsider,
                )
            )
        ).scalar_one()
        assert new_member.joined_at is not None
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
        assert audit[0].user_metadata["target_was_member"] is False

    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_idempotent_when_target_already_owner(
        self, ws_fixture, db_session
    ):
        s = ws_fixture
        svc = WorkspaceOwnershipService(db_session)
        # First a REAL force-transfer so the baseline epoch is non-zero (1) and one
        # audit row exists — otherwise asserting epoch==0 cannot distinguish a
        # correct no-op from the server_default the fixture row already carries.
        await svc.force_transfer_ownership(
            workspace_id=s.workspace_id,
            target_user_id=s.member,
            performed_by_user_id="admin_runner",
            performed_by_email="admin@kagura.invalid",
            reason="initial break-glass",
        )
        # Now a no-op: force-transfer to the CURRENT owner (member). No epoch bump,
        # no NEW audit row.
        result = await svc.force_transfer_ownership(
            workspace_id=s.workspace_id,
            target_user_id=s.member,
            performed_by_user_id="admin_runner",
            performed_by_email="admin@kagura.invalid",
            reason="noop",
        )
        assert result.changed is False
        assert result.ownership_epoch == 1  # unchanged from the first transfer, not bumped
        ws = await _workspace(db_session, s.workspace_id)
        assert ws.ownership_epoch == 1
        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.resource == f"workspace:{s.workspace_id}")
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1  # the no-op added NO new audit row

    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_nonexistent_target_raises(self, ws_fixture, db_session):
        s = ws_fixture
        with pytest.raises(BadRequestError):
            await WorkspaceOwnershipService(db_session).force_transfer_ownership(
                workspace_id=s.workspace_id,
                target_user_id="ghost_user_does_not_exist",
                performed_by_user_id="admin_runner",
                performed_by_email="admin@kagura.invalid",
                reason="typo target",
            )
        await db_session.rollback()
        ws = await _workspace(db_session, s.workspace_id)
        assert ws.owner_user_id == s.owner
        assert ws.ownership_epoch == 0

    @pytest.mark.asyncio(loop_scope="session")
    async def test_force_transfer_zero_member_workspace(self, db_session):
        # A workspace with owner_user_id set but ZERO member rows — break-glass to a
        # real user installs them as the OWNER member (the "no eligible target" case).
        owner = f"zoown_{uuid4().hex[:8]}"
        target = f"zotgt_{uuid4().hex[:8]}"
        workspace_id = uuid4()
        for uid in (owner, target):
            await _add_user(db_session, uid)
        await db_session.flush()
        db_session.add(
            Workspace(
                id=workspace_id,
                name=f"zeromember-{uuid4().hex[:8]}",
                plan_name="free",
                owner_user_id=owner,
                daily_api_limit=100,
                weekly_api_limit=500,
                deleted_at=None,
            )
        )
        await db_session.commit()  # NOTE: no WorkspaceMember rows at all
        try:
            result = await WorkspaceOwnershipService(db_session).force_transfer_ownership(
                workspace_id=workspace_id,
                target_user_id=target,
                performed_by_user_id="admin_runner",
                performed_by_email="admin@kagura.invalid",
                reason="zero-member rescue",
            )
            assert result.changed is True
            ws = await _workspace(db_session, workspace_id)
            assert ws.owner_user_id == target
            assert ws.ownership_epoch == 1
            assert await _role_of(db_session, workspace_id, target) == WorkspaceRole.OWNER
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
                User.__table__.delete().where(User.user_id.in_([owner, target]))
            )
            await db_session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_force_transfer_concurrent_preserves_single_owner(async_engine, db_session):
    # Two concurrent break-glass force-transfers (to different targets) from
    # independent sessions. force_transfer has NO current-owner check, so BOTH
    # succeed -- but the shared workspace row lock must SERIALIZE them: each epoch
    # bump applies (final epoch == 2, no lost increment) and there is exactly ONE
    # OWNER member at the end (no torn double-owner). Without the lock both could
    # read epoch==0, write epoch==1 (lost update), and both promote.
    owner = f"fcown_{uuid4().hex[:8]}"
    m1 = f"fcm1_{uuid4().hex[:8]}"
    m2 = f"fcm2_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    for uid in (owner, m1, m2):
        await _add_user(db_session, uid)
    await db_session.flush()
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"fconc-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    await db_session.flush()
    await _add_member(db_session, workspace_id, owner, WorkspaceRole.OWNER)
    await _add_member(db_session, workspace_id, m1, WorkspaceRole.MEMBER)
    await _add_member(db_session, workspace_id, m2, WorkspaceRole.MEMBER)
    await db_session.commit()

    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def _fxfer(target: str) -> str:
        async with session_maker() as s:
            await WorkspaceOwnershipService(s).force_transfer_ownership(
                workspace_id=workspace_id,
                target_user_id=target,
                performed_by_user_id="admin_runner",
                performed_by_email="admin@kagura.invalid",
                reason="concurrent break-glass",
            )
            return "ok"

    try:
        statuses = sorted(await asyncio.gather(_fxfer(m1), _fxfer(m2)))
        assert statuses == ["ok", "ok"]  # force has no current-owner conflict
        async with session_maker() as s:
            ws = (
                await s.execute(select(Workspace).where(Workspace.id == workspace_id))
            ).scalar_one()
            owners = (
                (
                    await s.execute(
                        select(WorkspaceMember).where(
                            WorkspaceMember.workspace_id == workspace_id,
                            WorkspaceMember.role == WorkspaceRole.OWNER,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(owners) == 1  # lock prevented a torn double-owner
            assert owners[0].user_id == ws.owner_user_id
            assert ws.owner_user_id in (m1, m2)
            assert ws.ownership_epoch == 2  # BOTH increments applied -- none lost
    finally:
        await db_session.execute(
            AuditLog.__table__.delete().where(AuditLog.resource == f"workspace:{workspace_id}")
        )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == workspace_id)
        )
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
        await db_session.execute(User.__table__.delete().where(User.user_id.in_([owner, m1, m2])))
        await db_session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_member_unique_constraint_rejects_duplicate(db_session):
    # #1101: (workspace_id, user_id) is UNIQUE at the DB level now, so the duplicate
    # membership row the break-glass ADD path could otherwise race into is rejected.
    from sqlalchemy.exc import IntegrityError

    owner = f"uqo_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    await _add_user(db_session, owner)
    await db_session.flush()
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"uq-{uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=owner,
            daily_api_limit=100,
            weekly_api_limit=500,
            deleted_at=None,
        )
    )
    await db_session.flush()
    await _add_member(db_session, workspace_id, owner, WorkspaceRole.OWNER)
    await db_session.commit()
    try:
        await _add_member(db_session, workspace_id, owner, WorkspaceRole.MEMBER)  # dup (ws, user)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()
    finally:
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id == workspace_id)
        )
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
        await db_session.execute(User.__table__.delete().where(User.user_id == owner))
        await db_session.commit()
