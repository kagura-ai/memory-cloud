"""Tests for services/system_admin_service.py.

Covers listing, promotion, demotion, and deletion-guard logic.
All tests use the real db_session fixture (async SQLAlchemy).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

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
        admins, initial_id = await service.list_system_admins()
        assert admins == []
        assert initial_id == 0

    @pytest.mark.asyncio
    async def test_single_admin(self, db_session, fixture_admin):
        service = SystemAdminService(db_session)
        admins, initial_id = await service.list_system_admins()
        assert len(admins) == 1
        assert admins[0].id == fixture_admin.id
        assert initial_id == fixture_admin.id

    @pytest.mark.asyncio
    async def test_multiple_admins_initial_preserved(
        self, db_session, fixture_admin, fixture_second_admin
    ):
        service = SystemAdminService(db_session)
        admins, initial_id = await service.list_system_admins()
        assert len(admins) == 2
        assert initial_id == fixture_admin.id

    @pytest.mark.asyncio
    async def test_fallback_first_admin_when_no_initial_flag(self, db_session):
        """If no admin has is_initial_admin=True, initial_id falls back to first admin."""
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
        admins, initial_id = await service.list_system_admins()
        assert initial_id == a1.id


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
        assert exc.value.error_code == "ADMIN-101"
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
        assert exc.value.error_code == "ADMIN-102"
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
    async def test_allowed_when_multiple_admins(
        self, db_session, fixture_admin, fixture_second_admin
    ):
        service = SystemAdminService(db_session)
        ok, reason = await service.can_delete_admin(fixture_second_admin.user_id)
        assert ok is True
        assert reason == ""
