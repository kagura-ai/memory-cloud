"""Tests for services/system_admin_service.py.

Covers listing, promotion, demotion, and deletion-guard logic.
All tests use the real db_session fixture (async SQLAlchemy).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.routes.admin import RoleUpdateRequest, update_user_role
from models.auth import AuditLog, User
from services.system_admin_service import SystemAdminService
from utils.exceptions import AdminProtectionError, BadRequestError, NotFoundException


@pytest_asyncio.fixture(autouse=True)
async def cleanup_admin_test_data(db_session):
    """Remove admin-test side-effects that survive db_session rollback
    because SystemAdminService methods commit internally.
    Clean-up runs both before and after each test since db_session is
    session-scoped and other tests may leak committed rows.
    """
    await db_session.execute(text("TRUNCATE TABLE audit_logs RESTART IDENTITY CASCADE"))
    await db_session.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
    await db_session.commit()
    try:
        yield
    finally:
        await db_session.execute(text("TRUNCATE TABLE audit_logs RESTART IDENTITY CASCADE"))
        await db_session.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        await db_session.commit()


@pytest_asyncio.fixture
async def fixture_admin(db_session):
    """Create a system admin user."""
    suffix = uuid4().hex[:8]
    user = User(
        email=f"admin-{suffix}@example.com",
        user_id=f"admin-{suffix}",
        role="admin",
        is_initial_admin=True,
        auth_provider="google",
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def fixture_regular(db_session):
    """Create a regular user."""
    suffix = uuid4().hex[:8]
    user = User(
        email=f"user-{suffix}@example.com",
        user_id=f"user-{suffix}",
        role="user",
        is_initial_admin=False,
        auth_provider="google",
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def fixture_second_admin(db_session):
    """Create a second system admin (not initial)."""
    suffix = uuid4().hex[:8]
    user = User(
        email=f"admin2-{suffix}@example.com",
        user_id=f"admin2-{suffix}",
        role="admin",
        is_initial_admin=False,
        auth_provider="github",
    )
    db_session.add(user)
    await db_session.flush()
    return user


class TestListSystemAdmins:
    @pytest.mark.asyncio
    async def test_empty_list(self, db_session):
        service = SystemAdminService(db_session)
        admins = await service.list_system_admins()
        assert admins == []

    @pytest.mark.asyncio
    async def test_single_admin(self, db_session, fixture_admin):
        service = SystemAdminService(db_session)
        admins = await service.list_system_admins()
        assert [a.user_id for a in admins] == [fixture_admin.user_id]
        assert admins[0].is_initial_admin is True

    @pytest.mark.asyncio
    async def test_multiple_admins_initial_preserved(
        self, db_session, fixture_admin, fixture_second_admin
    ):
        service = SystemAdminService(db_session)
        admins = await service.list_system_admins()
        assert len(admins) == 2
        initial = [a.user_id for a in admins if a.is_initial_admin]
        assert initial == [fixture_admin.user_id]

    @pytest.mark.asyncio
    async def test_no_admin_flagged_initial_when_none_is(self, db_session):
        """Without an is_initial_admin admin, none is reported as initial (#1813)."""
        suffix = uuid4().hex[:8]
        a1 = User(
            email=f"a1-{suffix}@example.com",
            user_id=f"a1-{suffix}",
            role="admin",
            is_initial_admin=False,
        )
        db_session.add(a1)
        await db_session.flush()

        service = SystemAdminService(db_session)
        admins = await service.list_system_admins()
        assert [a.user_id for a in admins] == [a1.user_id]
        assert not any(a.is_initial_admin for a in admins)


class TestPromoteToSystemAdmin:
    @pytest.mark.asyncio
    async def test_promote_success(self, db_session, fixture_regular):
        service = SystemAdminService(db_session)
        result = await service.promote_to_system_admin(
            fixture_regular.user_id, promoted_by="promoter@example.com"
        )
        assert result.role == "admin"

        # Verify via DB
        stmt = select(User).where(User.user_id == fixture_regular.user_id)
        user = (await db_session.execute(stmt)).scalar_one()
        assert user.role == "admin"

    @pytest.mark.asyncio
    async def test_promote_creates_audit_log(self, db_session, fixture_regular):
        service = SystemAdminService(db_session)
        await service.promote_to_system_admin(
            fixture_regular.user_id, promoted_by="promoter@example.com"
        )
        await db_session.flush()

        stmt = (
            select(AuditLog)
            .where(AuditLog.action == "system_admin_promote")
            .where(AuditLog.user_id == fixture_regular.user_id)
        )
        audit = (await db_session.execute(stmt)).scalar_one()
        assert audit.user_id == fixture_regular.user_id
        assert audit.old_value_hash == "user"
        assert audit.new_value_hash == "admin"
        assert audit.user_metadata["promoted_by"] == "promoter@example.com"

    @pytest.mark.asyncio
    async def test_promote_user_not_found_raises_404(self, db_session):
        service = SystemAdminService(db_session)
        with pytest.raises(NotFoundException) as exc:
            await service.promote_to_system_admin("nonexistent", "promoter@example.com")
        assert exc.value.status_code == 404
        assert exc.value.error_code == "RES-001"
        assert "User not found" in exc.value.message

    @pytest.mark.asyncio
    async def test_promote_already_admin_raises_400(self, db_session, fixture_admin):
        service = SystemAdminService(db_session)
        with pytest.raises(BadRequestError) as exc:
            await service.promote_to_system_admin(fixture_admin.user_id, "promoter@example.com")
        assert exc.value.status_code == 400
        assert exc.value.error_code == "REQ-101"
        assert "already a system admin" in exc.value.message


class TestDemoteSystemAdmin:
    @pytest.mark.asyncio
    async def test_demote_success(self, db_session, fixture_admin, fixture_second_admin):
        service = SystemAdminService(db_session)
        result = await service.demote_system_admin(
            fixture_second_admin.user_id, demoted_by="demoter@example.com"
        )
        assert result.role == "user"

    @pytest.mark.asyncio
    async def test_demote_creates_audit_log(self, db_session, fixture_admin, fixture_second_admin):
        service = SystemAdminService(db_session)
        await service.demote_system_admin(
            fixture_second_admin.user_id, demoted_by="demoter@example.com"
        )
        await db_session.flush()

        stmt = (
            select(AuditLog)
            .where(AuditLog.action == "system_admin_demote")
            .where(AuditLog.user_id == fixture_second_admin.user_id)
        )
        audit = (await db_session.execute(stmt)).scalar_one()
        assert audit.user_id == fixture_second_admin.user_id
        assert audit.old_value_hash == "admin"
        assert audit.new_value_hash == "user"

    @pytest.mark.asyncio
    async def test_demote_user_not_found_raises_404(self, db_session):
        service = SystemAdminService(db_session)
        with pytest.raises(NotFoundException) as exc:
            await service.demote_system_admin("nonexistent", "demoter@example.com")
        assert exc.value.status_code == 404
        assert exc.value.error_code == "RES-001"

    @pytest.mark.asyncio
    async def test_demote_not_admin_raises_400(self, db_session, fixture_regular):
        service = SystemAdminService(db_session)
        with pytest.raises(BadRequestError) as exc:
            await service.demote_system_admin(fixture_regular.user_id, "demoter@example.com")
        assert exc.value.status_code == 400
        assert exc.value.error_code == "REQ-102"
        assert "not a system admin" in exc.value.message

    @pytest.mark.asyncio
    async def test_demote_initial_admin_raises_403(self, db_session, fixture_admin):
        service = SystemAdminService(db_session)
        with pytest.raises(AdminProtectionError) as exc:
            await service.demote_system_admin(fixture_admin.user_id, "demoter@example.com")
        assert exc.value.status_code == 403
        assert exc.value.error_code == "ADMIN-001"
        assert exc.value.reason == "initial_admin"
        assert "initial" in exc.value.message.lower()

    @pytest.mark.asyncio
    async def test_demote_last_admin_raises_403(self, db_session):
        """Only one admin exists — cannot demote them."""
        suffix = uuid4().hex[:8]
        lone_admin = User(
            email=f"lone-admin-{suffix}@example.com",
            user_id=f"lone-admin-{suffix}",
            role="admin",
            is_initial_admin=False,
        )
        db_session.add(lone_admin)
        await db_session.flush()

        service = SystemAdminService(db_session)
        with pytest.raises(AdminProtectionError) as exc:
            await service.demote_system_admin(lone_admin.user_id, "demoter@example.com")
        assert exc.value.status_code == 403
        assert exc.value.error_code == "ADMIN-001"
        assert exc.value.reason == "last_admin"
        assert "last" in exc.value.message.lower()


OPERATOR = {"user_id": "operator:ops@example.com", "email": "operator:ops@example.com"}

# The two routes that take the admin role from an account (#2828). The role
# PUT moves an admin to ``user``, the only other system role (#2854).
ROUTES = ["put:user", "demote"]


async def _take_admin_role(db_session, route: str, user_id: str):
    """Demote ``user_id`` through the role PUT or through the demote route's service."""
    if route.startswith("put:"):
        request = RoleUpdateRequest(role=route.removeprefix("put:"))
        return await update_user_role(user_id, request, OPERATOR, db_session)
    return await SystemAdminService(db_session).demote_system_admin(user_id, OPERATOR["email"])


async def _stored(db_session, user_id: str) -> tuple[str | None, int]:
    """The role now stored for ``user_id`` and the number of audit rows, read fresh."""
    await db_session.rollback()
    role = await db_session.scalar(select(User.role).where(User.user_id == user_id))
    audits = await db_session.scalar(select(func.count()).select_from(AuditLog))
    return role, audits


class TestRoleChangeProtectionsAreTheSameOnBothRoutes:
    """#2828: the role PUT and the demote route share ``lock_for_role_change``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ROUTES)
    async def test_the_initial_admin_is_refused_and_nothing_changes(
        self, db_session, fixture_admin, fixture_second_admin, route
    ):
        initial_id = fixture_admin.user_id
        await db_session.commit()
        with pytest.raises(AdminProtectionError) as refused:
            await _take_admin_role(db_session, route, initial_id)
        assert refused.value.status_code == 403
        assert refused.value.error_code == "ADMIN-001"
        assert refused.value.reason == "initial_admin"
        assert refused.value.message == (
            "Cannot demote the initial system administrator. This is a protected account."
        )
        assert await _stored(db_session, initial_id) == ("admin", 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ROUTES)
    async def test_the_last_admin_is_refused_and_nothing_changes(
        self, db_session, fixture_second_admin, fixture_regular, route
    ):
        # One admin (not the initial one) beside a regular account.
        last_id = fixture_second_admin.user_id
        await db_session.commit()
        with pytest.raises(AdminProtectionError) as refused:
            await _take_admin_role(db_session, route, last_id)
        assert refused.value.status_code == 403
        assert refused.value.error_code == "ADMIN-001"
        assert refused.value.reason == "last_admin"
        assert refused.value.message == (
            "Cannot demote the last remaining system administrator. At least one admin must exist."
        )
        assert await _stored(db_session, last_id) == ("admin", 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["put:user", "demote"])
    async def test_a_second_admin_is_demoted_with_one_audit_row_in_one_commit(
        self, db_session, fixture_admin, fixture_second_admin, route
    ):
        target_id, target_email = fixture_second_admin.user_id, fixture_second_admin.email
        await db_session.commit()
        commits: list[object] = []
        on_commit = commits.append
        event.listen(db_session.sync_session, "after_commit", on_commit)
        try:
            await _take_admin_role(db_session, route, target_id)
        finally:
            event.remove(db_session.sync_session, "after_commit", on_commit)
        assert len(commits) == 1

        # What another session reads: the role and its audit row, together.
        maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
        async with maker() as other:
            role = await other.scalar(select(User.role).where(User.user_id == target_id))
            [audit] = (await other.execute(select(AuditLog))).scalars().all()
        assert role == "user"
        assert audit.old_value_hash == "admin"
        assert audit.new_value_hash == "user"
        assert audit.user_email == OPERATOR["email"]
        if route == "demote":
            assert audit.action == "system_admin_demote"
            assert audit.user_id == target_id
            assert audit.resource == f"user:{target_email}"
            assert audit.user_metadata == {
                "demoted_by": OPERATOR["email"],
                "was_initial_admin": False,
            }
        else:
            assert audit.action == "system_role_update"
            assert audit.user_id == OPERATOR["user_id"]
            assert audit.resource == f"user:{target_id}"
            assert audit.user_metadata == {
                "actor_user_id": OPERATOR["user_id"],
                "target_user_id": target_id,
                "old_role": "admin",
                "new_role": "user",
            }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ROUTES)
    async def test_an_unknown_user_is_404_and_nothing_is_written(
        self, db_session, fixture_admin, route
    ):
        admin_id = fixture_admin.user_id
        await db_session.commit()
        with pytest.raises((HTTPException, NotFoundException)) as missing:
            await _take_admin_role(db_session, route, "nonexistent")
        assert missing.value.status_code == 404
        assert await _stored(db_session, admin_id) == ("admin", 0)


class TestRolePutLeavesOtherChangesAlone:
    """#2828: a change that takes the admin role from nobody is not held back."""

    @pytest.mark.asyncio
    async def test_a_promotion_beside_the_last_admin_is_stored_with_its_audit_row(
        self, db_session, fixture_second_admin, fixture_regular
    ):
        target_id = fixture_regular.user_id
        await db_session.commit()
        answer = await update_user_role(
            target_id, RoleUpdateRequest(role="admin"), OPERATOR, db_session
        )
        assert answer == {
            "message": "User role updated to admin",
            "user_id": target_id,
            "new_role": "admin",
        }
        assert await _stored(db_session, target_id) == ("admin", 1)
        [audit] = (await db_session.execute(select(AuditLog))).scalars().all()
        assert audit.action == "system_role_update"
        assert audit.user_metadata == {
            "actor_user_id": OPERATOR["user_id"],
            "target_user_id": target_id,
            "old_role": "user",
            "new_role": "admin",
        }

    @pytest.mark.asyncio
    async def test_the_last_admin_can_be_set_to_admin_again(self, db_session, fixture_admin):
        admin_id = fixture_admin.user_id
        await db_session.commit()
        answer = await update_user_role(
            admin_id, RoleUpdateRequest(role="admin"), OPERATOR, db_session
        )
        assert answer["new_role"] == "admin"
        assert await _stored(db_session, admin_id) == ("admin", 1)


class TestCanDeleteAdmin:
    @pytest.mark.asyncio
    async def test_not_admin_returns_allowed(self, db_session, fixture_regular):
        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin(fixture_regular.user_id)
        assert ok is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_nonexistent_returns_allowed(self, db_session):
        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin("ghost")
        assert ok is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_initial_admin_blocked(self, db_session, fixture_admin):
        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin(fixture_admin.user_id)
        assert ok is False
        assert "initial" in reason.lower()

    @pytest.mark.asyncio
    async def test_last_admin_blocked(self, db_session):
        suffix = uuid4().hex[:8]
        lone_admin = User(
            email=f"lone-admin-{suffix}@example.com",
            user_id=f"lone-admin-{suffix}",
            role="admin",
            is_initial_admin=False,
        )
        db_session.add(lone_admin)
        await db_session.flush()

        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin(lone_admin.user_id)
        assert ok is False
        assert "last" in reason.lower()

    @pytest.mark.asyncio
    async def test_the_two_refusals_keep_their_exact_answers(
        self, db_session, fixture_admin, fixture_regular
    ):
        # #2828 only respelled the admin predicate of the count.
        service = SystemAdminService(db_session)
        assert await service.can_delete_admin(fixture_admin.user_id) == (
            False,
            "Cannot delete the initial system administrator",
        )
        fixture_admin.is_initial_admin = False
        await db_session.flush()
        assert await service.can_delete_admin(fixture_admin.user_id) == (
            False,
            "Cannot delete the last remaining system administrator",
        )

    @pytest.mark.asyncio
    async def test_allowed_when_multiple_admins(
        self, db_session, fixture_admin, fixture_second_admin
    ):
        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin(fixture_second_admin.user_id)
        assert ok is True
        assert reason == ""
