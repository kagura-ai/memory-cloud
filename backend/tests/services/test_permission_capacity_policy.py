"""#1948: the capacity-over lock lives in the context resolvers.

``PermissionService.resolve_context_for_workspace_read`` and
``check_context_access`` (hence ``check_context_write`` / ``check_context_owner``)
take a required ``capacity`` policy. ``BLOCKED`` runs the #1941 lock after every
authorization step (so the lock is never an existence oracle); ``ALLOWED`` never
touches it. The argument has no default on purpose: a new caller must decide.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.workspace_roles import ContextRole, WorkspaceRole
from services.capacity_lock import CapacityPolicy
from services.permission_service import PermissionService
from utils.exceptions import CapacityLockedError, NotFoundException

pytestmark = pytest.mark.usefixtures("no_identity_links")

WS = uuid4()
CTX = uuid4()
USER = "u1"


def _locked() -> CapacityLockedError:
    return CapacityLockedError(
        memory_count=1001,
        memory_limit=1000,
        over_memories=1,
        used_bytes=0,
        storage_limit_bytes=1,
        over_bytes=0,
        cleanup_url="https://app.example.test/workspace/settings/plan",
    )


def _lock(side_effect=None):
    return patch(
        "services.permission_service.ensure_not_capacity_locked",
        AsyncMock(side_effect=side_effect),
    )


def _context(*, is_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=CTX, workspace_id=WS, is_private=is_private, created_by=USER)


class TestSignature:
    def test_the_policy_is_required_on_every_resolver(self) -> None:
        for name in (
            "resolve_context_for_workspace_read",
            "check_context_access",
            "check_context_write",
            "check_context_owner",
        ):
            param = inspect.signature(getattr(PermissionService, name)).parameters["capacity"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name

    def test_the_vocabulary_is_the_dispatchers(self) -> None:
        assert {p.value for p in CapacityPolicy} == {"blocked", "allowed"}


class TestResolveForWorkspaceRead:
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
        return svc

    @pytest.mark.asyncio
    async def test_blocked_checks_the_lock_after_authorization(self, service) -> None:
        order: list[str] = []
        service.check_workspace_access.side_effect = lambda **_: (
            order.append("membership"),
            SimpleNamespace(role=WorkspaceRole.OWNER, allowed_context_ids=None),
        )[1]
        service._apply_agent_binding_filter.side_effect = lambda *_a, **_k: (
            order.append("binding"),
            None,
        )[1]

        async def locked(*_a, **_k):
            order.append("capacity")
            raise _locked()

        with _lock(locked) as ensure, pytest.raises(CapacityLockedError):
            await service.resolve_context_for_workspace_read(
                user_id=USER, context_id=CTX, capacity=CapacityPolicy.BLOCKED
            )
        assert order == ["membership", "binding", "capacity"]
        assert ensure.await_args.args[1] == WS
        assert ensure.await_args.kwargs == {"user_id": USER}

    @pytest.mark.asyncio
    async def test_allowed_never_reads_the_lock(self, service) -> None:
        with _lock() as ensure:
            context = await service.resolve_context_for_workspace_read(
                user_id=USER, context_id=CTX, capacity=CapacityPolicy.ALLOWED
            )
        assert context.id == CTX
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_denied_caller_never_reaches_the_lock(self, service) -> None:
        """Authorization first: the lock is not an existence oracle."""
        service.check_workspace_access.side_effect = None
        service.check_workspace_access = AsyncMock(
            side_effect=__import__(
                "utils.exceptions", fromlist=["AuthorizationError"]
            ).AuthorizationError("no")
        )
        with _lock(_locked()) as ensure, pytest.raises(NotFoundException):
            await service.resolve_context_for_workspace_read(
                user_id=USER, context_id=CTX, capacity=CapacityPolicy.BLOCKED
            )
        ensure.assert_not_awaited()


class TestCheckContextAccess:
    @pytest.fixture
    def service(self):
        svc = PermissionService(MagicMock())
        svc._check_context_access_rbac = AsyncMock(return_value=(_context(), ContextRole.OWNER))
        svc._apply_agent_binding_filter = AsyncMock(return_value=None)
        return svc

    @pytest.mark.asyncio
    async def test_blocked_checks_the_lock_after_rbac_and_binding(self, service) -> None:
        with _lock(_locked()) as ensure, pytest.raises(CapacityLockedError):
            await service.check_context_access(USER, CTX, capacity=CapacityPolicy.BLOCKED)
        service._check_context_access_rbac.assert_awaited_once()
        service._apply_agent_binding_filter.assert_awaited_once()
        assert ensure.await_args.args[1] == WS

    @pytest.mark.asyncio
    async def test_allowed_never_reads_the_lock(self, service) -> None:
        with _lock() as ensure:
            context, role = await service.check_context_access(
                USER, CTX, capacity=CapacityPolicy.ALLOWED
            )
        assert (context.id, role) == (CTX, ContextRole.OWNER)
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_and_owner_wrappers_forward_the_policy(self, service) -> None:
        with _lock(_locked()), pytest.raises(CapacityLockedError):
            await service.check_context_write(USER, CTX, capacity=CapacityPolicy.BLOCKED)
        with _lock(_locked()), pytest.raises(CapacityLockedError):
            await service.check_context_owner(USER, CTX, capacity=CapacityPolicy.BLOCKED)
        with _lock(_locked()) as ensure:
            await service.check_context_write(USER, CTX, capacity=CapacityPolicy.ALLOWED)
            await service.check_context_owner(USER, CTX, capacity=CapacityPolicy.ALLOWED)
        ensure.assert_not_awaited()
