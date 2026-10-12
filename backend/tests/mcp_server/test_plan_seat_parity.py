"""#1943 on the MCP face: content writes ask for a writable slot, reads do not.

``_resolve_context_for_read(..., access="write")`` is what the write tools
(set_state, record_measurement, feedback) pass; the edge tools pass the same
to ``_resolve_context``. The slot check runs after the resolver, so an
unreachable context still answers its uniform not-found first.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _resolve_context, _resolve_context_for_read
from utils.exceptions import FeatureNotAvailableError


def _refused() -> FeatureNotAvailableError:
    return FeatureNotAvailableError("Feature 'contexts' is paused", gate="plan", feature="contexts")


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["read", "write"])
async def test_read_resolver_asks_for_a_slot_only_on_write(access: str) -> None:
    context = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    with (
        patch("services.permission_service.PermissionService") as perm_cls,
        patch(
            "services.plan_suspension.ensure_context_writable", AsyncMock(side_effect=_refused())
        ) as slot,
    ):
        perm_cls.return_value.resolve_context_for_workspace_read = AsyncMock(return_value=context)
        if access == "write":
            with pytest.raises(FeatureNotAvailableError):
                await _resolve_context_for_read(AsyncMock(), "u", context.id, access=access)
            assert slot.await_args.args[1] is context
        else:
            assert await _resolve_context_for_read(AsyncMock(), "u", context.id) is context
            slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_resolver_asks_for_a_slot_on_write() -> None:
    context = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    with (
        patch(
            "services.context_service.ContextService.get_context",
            AsyncMock(return_value=context),
        ),
        patch(
            "services.plan_suspension.ensure_context_writable", AsyncMock(side_effect=_refused())
        ) as slot,
    ):
        with pytest.raises(FeatureNotAvailableError):
            await _resolve_context(MagicMock(), "u", context.id, access="write")
        assert slot.await_args.args[1] is context
        slot.reset_mock()
        assert await _resolve_context(MagicMock(), "u", context.id) is context
        slot.assert_not_awaited()
