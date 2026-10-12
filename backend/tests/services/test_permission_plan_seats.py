"""#1943: where the seat and slot checks sit in PermissionService.

* ``check_workspace_access`` refuses a member without a seat AFTER the
  membership and role checks (a non-member keeps its uniform refusal).
* ``_check_context_access_rbac`` does the same right after it reads the
  membership row (before the owner/admin bypass).
* The context resolvers refuse a write into a context without a slot after the
  #1941 capacity lock, only for ``CapacityPolicy.BLOCKED`` + write access.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.workspace_roles import ContextRole, WorkspaceRole
from services.capacity_lock import CapacityPolicy
from services.permission_service import PermissionService
from utils.exceptions import AuthorizationError, FeatureNotAvailableError, NotFoundException

pytestmark = pytest.mark.usefixtures("no_identity_links")

WS = uuid4()
CTX = uuid4()
USER = "u1"


def _refused() -> FeatureNotAvailableError:
    return FeatureNotAvailableError("Feature 'members' is paused", gate="plan", feature="members")


def _seat(side_effect=None):
    return patch(
        "services.permission_service.ensure_member_seated", AsyncMock(side_effect=side_effect)
    )


def _slot(side_effect=None):
    return patch(
        "services.permission_service.ensure_context_writable", AsyncMock(side_effect=side_effect)
    )


def _context(*, is_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=CTX, workspace_id=WS, is_private=is_private, created_by=USER)


class TestWorkspaceAccess:
    @pytest.fixture
    def service(self):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(id=WS, deleted_at=None)
        db.execute = AsyncMock(return_value=result)
        svc = PermissionService(db)
        svc.workspace_service.get_member = AsyncMock(
            return_value=SimpleNamespace(id=7, role=WorkspaceRole.MEMBER, allowed_context_ids=[])
        )
        return svc

    @pytest.mark.asyncio
    async def test_a_member_without_a_seat_is_refused_after_the_role_check(self, service) -> None:
        with _seat(_refused()) as seat, pytest.raises(FeatureNotAvailableError):
            await service.check_workspace_access(USER, WS, required_role=WorkspaceRole.MEMBER)
        workspace, member = seat.await_args.args[1], seat.await_args.args[2]
        assert workspace.id == WS and member.id == 7

    @pytest.mark.asyncio
    async def test_a_role_too_low_is_refused_before_the_seat_is_read(self, service) -> None:
        with _seat(_refused()) as seat, pytest.raises(AuthorizationError):
            await service.check_workspace_access(USER, WS, required_role=WorkspaceRole.ADMIN)
        seat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_non_member_never_reaches_the_seat(self, service) -> None:
        service.workspace_service.get_member = AsyncMock(return_value=None)
        with _seat(_refused()) as seat, pytest.raises(AuthorizationError):
            await service.check_workspace_access(USER, WS)
        seat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_seated_member_passes(self, service) -> None:
        with _seat() as seat:
            member = await service.check_workspace_access(USER, WS)
        assert member.id == 7
        seat.assert_awaited_once()


class TestContextRbac:
    @pytest.fixture
    def service(self):
        db = MagicMock()
        ctx_result = MagicMock()
        ctx_result.scalar_one_or_none.return_value = _context()
        db.execute = AsyncMock(return_value=ctx_result)
        db.get = AsyncMock(return_value=SimpleNamespace(id=WS))
        svc = PermissionService(db)
        svc.workspace_service.get_member = AsyncMock(
            return_value=SimpleNamespace(id=7, role=WorkspaceRole.ADMIN, allowed_context_ids=None)
        )
        return svc

    @pytest.mark.asyncio
    async def test_the_seat_is_checked_before_the_admin_bypass(self, service) -> None:
        with _seat(_refused()), pytest.raises(FeatureNotAvailableError):
            await service._check_context_access_rbac(USER, CTX, ContextRole.VIEWER)

    @pytest.mark.asyncio
    async def test_a_private_context_of_the_creator_skips_membership_and_seat(
        self, service
    ) -> None:
        """The creator shortcut has never read membership (#165); the seat does not change it."""
        service.db.execute.return_value.scalar_one_or_none.return_value = _context(is_private=True)
        with _seat(_refused()) as seat:
            context, role = await service._check_context_access_rbac(USER, CTX, ContextRole.OWNER)
        assert role is ContextRole.OWNER
        seat.assert_not_awaited()


class TestWriteSlots:
    @pytest.fixture
    def service(self):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = _context()
        db.execute = AsyncMock(return_value=result)
        svc = PermissionService(db)
        svc.check_workspace_access = AsyncMock(
            return_value=SimpleNamespace(role=WorkspaceRole.OWNER, allowed_context_ids=None)
        )
        svc._apply_agent_binding_filter = AsyncMock(return_value=None)
        svc._check_context_access_rbac = AsyncMock(return_value=(_context(), ContextRole.OWNER))
        return svc

    @pytest.mark.asyncio
    async def test_a_blocked_write_checks_the_slot_after_the_lock(self, service) -> None:
        order: list[str] = []

        async def lock(*_a, **_k):
            order.append("capacity")

        async def slot(*_a, **_k):
            order.append("slot")
            raise _refused()

        with (
            patch(
                "services.permission_service.ensure_not_capacity_locked",
                AsyncMock(side_effect=lock),
            ),
            _slot(slot),
            pytest.raises(FeatureNotAvailableError),
        ):
            await service.resolve_context_for_workspace_read(
                user_id=USER,
                context_id=CTX,
                required_role=WorkspaceRole.MEMBER,  # write access
                capacity=CapacityPolicy.BLOCKED,
            )
        assert order == ["capacity", "slot"]

    @pytest.mark.asyncio
    async def test_a_blocked_read_never_asks_for_a_slot(self, service) -> None:
        with (
            patch("services.permission_service.ensure_not_capacity_locked", AsyncMock()),
            _slot(_refused()) as slot,
        ):
            await service.resolve_context_for_workspace_read(
                user_id=USER, context_id=CTX, capacity=CapacityPolicy.BLOCKED
            )
            await service.check_context_access(USER, CTX, capacity=CapacityPolicy.BLOCKED)
        slot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_allowed_write_never_asks_for_a_slot(self, service) -> None:
        with _slot(_refused()) as slot:
            await service.check_context_write(USER, CTX, capacity=CapacityPolicy.ALLOWED)
        slot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_check_context_write_blocked_asks_for_a_slot(self, service) -> None:
        with (
            patch("services.permission_service.ensure_not_capacity_locked", AsyncMock()),
            _slot(_refused()),
            pytest.raises(FeatureNotAvailableError),
        ):
            await service.check_context_write(USER, CTX, capacity=CapacityPolicy.BLOCKED)

    @pytest.mark.asyncio
    async def test_a_denied_caller_never_reaches_the_slot(self, service) -> None:
        service.check_workspace_access = AsyncMock(side_effect=AuthorizationError("no"))
        with _slot(_refused()) as slot, pytest.raises(NotFoundException):
            await service.resolve_context_for_workspace_read(
                user_id=USER,
                context_id=CTX,
                required_role=WorkspaceRole.MEMBER,
                capacity=CapacityPolicy.BLOCKED,
            )
        slot.assert_not_awaited()
